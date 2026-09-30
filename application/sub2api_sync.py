"""把 ChatGPT 账号导入 Sub2API：账号列表手动勾选导入 + 后台定时导入新增账号。

sub2api 的 POST /api/v1/admin/accounts/data 不按名字去重——同一个账号推两次就是两条。
所以「推过没有」只能记在本地：导入成功后往账号 overview 写 sub2api_synced_at，
手动和定时两条路都跳过带这个标记的账号。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from application.account_exports import CHATGPT_PLATFORM, _make_sub2api_json
from core.config_store import config_store
from domain.accounts import AccountExportSelection, AccountRecord, AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository

SYNCED_AT_KEY = "sub2api_synced_at"
ACCOUNT_ID_KEY = "sub2api_account_id"
IMPORT_GROUP_ID_KEY = "sub2api_import_group_id"
IMPORT_KNOWN_IDS_KEY = "sub2api_import_known_ids"
IMPORT_FINGERPRINT_KEY = "sub2api_import_fingerprint"
IMPORT_IDENTITY_KEY = "sub2api_import_identity"
IMPORT_MARKER_KEY = "sub2api_import_marker"
IMPORT_RECONCILE_BLOCKED_KEY = "sub2api_import_reconcile_blocked"
IMPORT_ATTEMPTED_AT_KEY = "sub2api_import_attempted_at"
IMPORT_MARKER_FIELD = "any_auto_register_import_marker"
INSTANCE_URL_KEY = "sub2api_instance_url"
REFRESHED_AT_KEY = "sub2api_refreshed_at"
MODELS_SYNCED_AT_KEY = "sub2api_models_synced_at"
SUB2API_GROUP_ID_KEY = "sub2api_group_id"
GROUP_BOUND_AT_KEY = "sub2api_group_bound_at"
AUTO_SYNC_SINCE_KEY = "sub2api_auto_sync_since"
AUTO_SYNC_STATUSES = {"registered", "trial", "subscribed"}
DEFAULT_INTERVAL_MINUTES = 10
REQUEST_TIMEOUT_SECONDS = 20
MAX_CONSECUTIVE_FAILURES = 3
AUTO_SYNC_TICK_SECONDS = 60
# 上次导入请求可能还在 Sub2API 那边处理；过了这段时间仍查不到带标记的记录，才认定上次没建成。
IMPORT_RETRY_GRACE_SECONDS = max(300, 3 * REQUEST_TIMEOUT_SECONDS)

# 手动和定时可能同时跑；不串行的话同一个账号会在两边各推一次，而 sub2api 不去重。
_SYNC_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    # SQLite 读回来的是 naive datetime，库里存的是 UTC。
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _idempotency_key(item: AccountRecord, data: dict, group_id: str, import_marker: str = "") -> str:
    # 同一导入尝试保持相同 key；Sub2API 拒绝同 key 的不同 payload，避免结果未明时重复创建。
    payload_key = import_marker or json.dumps(data, sort_keys=True, ensure_ascii=False)
    raw = f"{item.id}:{group_id}:{payload_key}"
    return "aar-sub2api-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def _sub2api_payload(item: AccountRecord, import_marker: str = "") -> dict:
    data = _make_sub2api_json(item)
    if import_marker:
        account = data["accounts"][0]
        extra = dict(account.get("extra") or {})
        extra[IMPORT_MARKER_FIELD] = import_marker
        account["extra"] = extra
    return data


def _import_fingerprint(item: AccountRecord, import_marker: str = "") -> str:
    data = _sub2api_payload(item, import_marker)
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_token(data: dict) -> bool:
    accounts = data.get("accounts") or [{}]
    credentials = accounts[0].get("credentials") or {}
    return bool(credentials.get("access_token") or credentials.get("refresh_token"))


class Sub2ApiClientError(RuntimeError):
    """Sub2API responded with an unusable result or could not be reached."""


class Sub2ApiImportRejected(Sub2ApiClientError):
    """The import was definitely rejected without creating an account."""


class Sub2ApiClient:
    def __init__(self, base_url: str, admin_key: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS):
        self.base_url = base_url.strip().rstrip("/")
        self.admin_key = admin_key.strip()
        self.timeout = timeout

    def list_groups(self) -> list[dict]:
        try:
            resp = requests.get(
                f"{self.base_url}/api/v1/admin/groups/all",
                headers={"x-api-key": self.admin_key},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise Sub2ApiClientError(f"请求 Sub2API 分组失败: {exc}") from exc
        try:
            body = resp.json()
        except Exception as exc:
            raise Sub2ApiClientError(f"Sub2API 分组响应格式无效: HTTP {resp.status_code}") from exc
        if resp.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
            message = body.get("message") if isinstance(body, dict) else resp.text[:200]
            raise Sub2ApiClientError(f"获取 Sub2API 分组失败: HTTP {resp.status_code}: {message or resp.text[:200]}")
        groups = body.get("data")
        if not isinstance(groups, list):
            raise Sub2ApiClientError("Sub2API 分组响应格式无效")
        return [group for group in groups if isinstance(group, dict) and group.get("id") is not None]

    def import_account(self, item: AccountRecord, group_id: str, import_marker: str = "") -> tuple[bool, str]:
        data = _sub2api_payload(item, import_marker)
        if not _has_token(data):
            raise Sub2ApiImportRejected("账号缺少 access_token / refresh_token")
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/admin/accounts/data",
                json={"data": data, "group_ids": [int(group_id)], "skip_default_group_bind": True},
                headers={
                    "x-api-key": self.admin_key,
                    "Idempotency-Key": _idempotency_key(item, data, group_id, import_marker),
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            return False, f"请求失败: {exc}"
        try:
            body = resp.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code != 200:
            message = f"HTTP {resp.status_code}: {body.get('message') or resp.text[:200]}"
            if resp.status_code < 500 and resp.status_code not in {408, 409, 429}:
                raise Sub2ApiImportRejected(message)
            return False, message
        if body.get("code") != 0:
            raise Sub2ApiImportRejected(str(body.get("message") or "Sub2API 拒绝导入"))
        # HTTP 200 不等于建成了：校验失败的账号只进 errors，account_created 是 0。
        result = body.get("data") or {}
        if int(result.get("account_created") or 0) < 1:
            errors = result.get("errors") or []
            message = (errors[0] or {}).get("message") if errors else ""
            raise Sub2ApiImportRejected(str(message or "sub2api 未创建账号"))
        return True, ""

    def list_matching_account_ids(
        self,
        item: AccountRecord,
        *,
        identity: str | None = None,
        import_marker: str | None = None,
    ) -> set[int]:
        search_identity = identity or item.email
        page = 1
        pages = 1
        account_ids: set[int] = set()
        while page <= pages:
            try:
                resp = requests.get(
                    f"{self.base_url}/api/v1/admin/accounts",
                    params={"page": page, "page_size": 100, "search": search_identity},
                    headers={"x-api-key": self.admin_key},
                    timeout=self.timeout,
                )
                body = resp.json()
            except Exception as exc:
                raise ValueError(f"查询 Sub2API 账号失败: {exc}") from exc
            if resp.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
                message = body.get("message") if isinstance(body, dict) else resp.text[:200]
                raise ValueError(f"查询 Sub2API 账号失败: HTTP {resp.status_code}: {message or resp.text[:200]}")
            result = body.get("data")
            rows = result.get("items") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Sub2API 账号列表响应格式无效")
            try:
                pages = max(1, int(result.get("pages") or 1))
            except (TypeError, ValueError) as exc:
                raise ValueError("Sub2API 账号列表分页信息无效") from exc
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_identity = str(row.get("name") or row.get("email") or "").strip()
                if row_identity.casefold() != search_identity.casefold() or row.get("id") is None:
                    continue
                extra = row.get("extra") or {}
                if import_marker and (not isinstance(extra, dict) or extra.get(IMPORT_MARKER_FIELD) != import_marker):
                    continue
                try:
                    account_ids.add(int(row["id"]))
                except (TypeError, ValueError):
                    raise ValueError("Sub2API 账号 ID 格式无效")
            page += 1
        return account_ids

    def find_new_account_id(
        self,
        item: AccountRecord,
        known_ids: set[int],
        *,
        identity: str | None = None,
        import_marker: str | None = None,
    ) -> int | None:
        new_ids = self.list_matching_account_ids(item, identity=identity, import_marker=import_marker) - known_ids
        return next(iter(new_ids)) if len(new_ids) == 1 else None

    def _account_action(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = getattr(requests, method)(
                f"{self.base_url}{path}",
                headers={"x-api-key": self.admin_key},
                timeout=self.timeout,
                **kwargs,
            )
            body = resp.json()
        except Exception as exc:
            raise ValueError(f"Sub2API 操作请求失败: {exc}") from exc
        if resp.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
            message = body.get("message") if isinstance(body, dict) else resp.text[:200]
            raise ValueError(f"Sub2API 操作失败: HTTP {resp.status_code}: {message or resp.text[:200]}")
        return body

    def _post_account_action(self, path: str) -> dict:
        return self._account_action("post", path)

    def bind_account_group(self, account_id: int, group_id: str) -> None:
        try:
            resp = requests.put(
                f"{self.base_url}/api/v1/admin/accounts/{account_id}",
                json={"group_ids": [int(group_id)]},
                headers={"x-api-key": self.admin_key},
                timeout=self.timeout,
            )
            body = resp.json()
        except Exception as exc:
            raise ValueError(f"Sub2API 分组绑定请求失败: {exc}") from exc
        if resp.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
            message = body.get("message") if isinstance(body, dict) else resp.text[:200]
            raise ValueError(f"Sub2API 分组绑定失败: HTTP {resp.status_code}: {message or resp.text[:200]}")

    def refresh_account(self, account_id: int) -> None:
        self._post_account_action(f"/api/v1/admin/accounts/{account_id}/refresh")

    def sync_upstream_models(self, account_id: int) -> None:
        # sync-upstream 只返回模型列表（并写能力元数据）；账号可用模型白名单
        # credentials.model_mapping 由 Sub2API 前端拿结果后自行保存，这里照做：
        # 先清空（整体替换），再写入上游列表。GET 回来的凭据已脱敏，PUT 时服务端保留 token。
        body = self._post_account_action(f"/api/v1/admin/accounts/{account_id}/models/sync-upstream")
        data = body.get("data")
        models = [str(m).strip() for m in (data.get("models") if isinstance(data, dict) else None) or []]
        models = list(dict.fromkeys(m for m in models if m))
        if not models:
            raise ValueError("Sub2API 未返回上游支持的模型")
        path = f"/api/v1/admin/accounts/{account_id}"
        account = self._account_action("get", path).get("data")
        credentials = dict(account.get("credentials") or {}) if isinstance(account, dict) else {}
        credentials["model_mapping"] = {model: model for model in models}
        self._account_action("put", path, json={"credentials": credentials})


class Sub2ApiSyncService:
    def __init__(
        self,
        repository: AccountsRepository | None = None,
        client_factory: Callable[[str, str], Sub2ApiClient] | None = None,
    ):
        self.repository = repository or AccountsRepository()
        self._client_factory = client_factory or Sub2ApiClient

    @staticmethod
    def _configured_base_url(settings: dict) -> str:
        return str(settings.get("sub2api_url", "") or "").strip().rstrip("/")

    def _client(self, settings: dict | None = None) -> Sub2ApiClient:
        settings = settings if settings is not None else config_store.get_all()
        base_url = self._configured_base_url(settings)
        admin_key = str(settings.get("sub2api_admin_key", "") or "").strip()
        if not base_url or not admin_key:
            raise ValueError("未配置 Sub2API 地址或 Admin API Key，请先到 设置 → ChatGPT → Sub2API 填写")
        return self._client_factory(base_url, admin_key)

    def list_groups(self) -> list[dict]:
        settings = config_store.get_all()
        return self._client(settings).list_groups()

    @staticmethod
    def _default_group_id(settings: dict) -> str:
        group_id = str(settings.get("sub2api_default_group_id", "") or "").strip()
        if not group_id:
            raise ValueError("请先到 设置 → ChatGPT → Sub2API 配置默认分组")
        try:
            int(group_id)
        except ValueError as exc:
            raise ValueError("Sub2API 默认分组无效，请重新选择") from exc
        return group_id

    def sync_selected(self, selection: AccountExportSelection) -> dict:
        if not selection.select_all and not selection.ids:
            # select_for_export 在 ids 为空时返回全部账号：导出无害，这里会把整库推过去。
            raise ValueError("请先勾选要导入的账号")
        selection.platform = selection.platform or CHATGPT_PLATFORM
        if selection.platform != CHATGPT_PLATFORM:
            raise ValueError("仅支持 ChatGPT 账号导入 Sub2API")
        settings = config_store.get_all()
        client = self._client(settings)
        group_id = self._default_group_id(settings)
        instance_url = self._configured_base_url(settings)
        with _SYNC_LOCK:
            items = self.repository.select_for_export(selection)
            return self._push(client, items, group_id, instance_url)

    def sync_new_accounts(self) -> dict | None:
        """定时入口。未开启返回 None；第一次看到开启只记起点、不推。"""
        settings = config_store.get_all()
        if str(settings.get("sub2api_auto_sync", "") or "").strip() != "1":
            if settings.get(AUTO_SYNC_SINCE_KEY, ""):
                # 关掉时清起点：下次开启只认那之后新增的，不把关闭期间的补推上去。
                config_store.set(AUTO_SYNC_SINCE_KEY, "")
            return None
        since_text = str(settings.get(AUTO_SYNC_SINCE_KEY, "") or "").strip()
        if not since_text:
            # 存量账号走手动导入；定时只管开启之后新增的。
            config_store.set(AUTO_SYNC_SINCE_KEY, _utcnow().isoformat())
            return self._empty_summary()
        since = _as_utc(datetime.fromisoformat(since_text))
        client = self._client(settings)
        group_id = self._default_group_id(settings)
        instance_url = self._configured_base_url(settings)
        with _SYNC_LOCK:
            items = self.repository.select_for_export(
                AccountExportSelection(platform=CHATGPT_PLATFORM, select_all=True),
                include_failed=True,
            )
            items = [
                item for item in items
                if (
                    item.lifecycle_status in AUTO_SYNC_STATUSES
                    and (_as_utc(item.created_at) or since) >= since
                ) or self._has_pending_sub2api_work(item)
            ]
            return self._push(client, items, group_id, instance_url)

    @staticmethod
    def _has_pending_sub2api_work(item: AccountRecord) -> bool:
        overview = item.overview or {}
        if overview.get(SYNCED_AT_KEY):
            if overview.get(ACCOUNT_ID_KEY):
                group_pending = bool(overview.get(SUB2API_GROUP_ID_KEY) and not overview.get(GROUP_BOUND_AT_KEY))
                return group_pending or not (overview.get(REFRESHED_AT_KEY) and overview.get(MODELS_SYNCED_AT_KEY))
            return bool(overview.get(IMPORT_KNOWN_IDS_KEY))
        return bool(overview.get(IMPORT_GROUP_ID_KEY) and overview.get(IMPORT_KNOWN_IDS_KEY))

    @staticmethod
    def _empty_summary(total: int = 0) -> dict:
        return {
            "total": total,
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "group_bind_failed": 0,
            "refresh_failed": 0,
            "model_sync_failed": 0,
            "postprocess_failed": 0,
            "deleted": 0,
            "aborted": False,
            "errors": [],
        }

    def _record_error(self, summary: dict, item: AccountRecord, stage: str, message: str) -> None:
        summary["errors"].append({"id": item.id, "email": item.email, "stage": stage, "message": message})

    def _is_same_instance(
        self,
        item: AccountRecord,
        overview: dict,
        summary: dict,
        failure_key: str,
        current_url: str,
    ) -> bool:
        source_url = str(overview.get(INSTANCE_URL_KEY) or "").strip().rstrip("/")
        if source_url and source_url == current_url:
            return True
        summary[failure_key] += 1
        message = (
            "Sub2API 实例地址已变化，"
            "无法确认远端账号 ID 的所属实例；请切回导入时的地址后重试"
            if source_url else
            "缺少导入时的 Sub2API 实例地址，拒绝使用当前实例操作远端账号"
        )
        self._record_error(summary, item, "instance_mismatch", message)
        return False

    def _postprocess(self, client: Sub2ApiClient, item: AccountRecord, overview: dict, summary: dict) -> None:
        account_id = int(overview[ACCOUNT_ID_KEY])
        group_id = str(overview.get(SUB2API_GROUP_ID_KEY) or "")
        stages = []
        if group_id:
            stages.append(("group_bind", GROUP_BOUND_AT_KEY, "group_bind_failed", lambda: client.bind_account_group(account_id, group_id)))
        stages.extend((
            ("refresh", REFRESHED_AT_KEY, "refresh_failed", lambda: client.refresh_account(account_id)),
            ("model_sync", MODELS_SYNCED_AT_KEY, "model_sync_failed", lambda: client.sync_upstream_models(account_id)),
        ))
        for stage, done_key, failure_key, action in stages:
            if overview.get(done_key):
                continue
            try:
                action()
            except Exception as exc:
                summary[failure_key] += 1
                self._record_error(summary, item, stage, str(exc))
                continue
            completed_at = _utcnow().isoformat()
            self.repository.update(item.id, AccountUpdateCommand(overview={done_key: completed_at}))
            overview[done_key] = completed_at
        if all(overview.get(done_key) for _, done_key, _, _ in stages) and self._delete_after_import_enabled():
            if self.repository.delete(item.id):
                summary["deleted"] += 1

    @staticmethod
    def _delete_after_import_enabled() -> bool:
        # 未保存过该配置视为开启；只有显式设为 "0" 才保留本地账号。
        return str(config_store.get("sub2api_delete_after_import", "") or "").strip() != "0"

    @staticmethod
    def _stored_import_ids(overview: dict) -> set[int] | None:
        raw = overview.get(IMPORT_KNOWN_IDS_KEY)
        if raw is None or raw == "":
            return None
        try:
            values = json.loads(raw)
            return {int(value) for value in values}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("本地 Sub2API 导入快照无效") from exc

    @staticmethod
    def _attempted_at(overview: dict) -> datetime | None:
        # 缺失或无法解析都当作「很久以前」：旧版本留下的挂起尝试没有这个键。
        raw = str(overview.get(IMPORT_ATTEMPTED_AT_KEY) or "").strip()
        if not raw:
            return None
        try:
            return _as_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None

    def _clear_pending_import(self, item: AccountRecord, overview: dict) -> None:
        keys = (
            IMPORT_GROUP_ID_KEY,
            IMPORT_KNOWN_IDS_KEY,
            IMPORT_FINGERPRINT_KEY,
            IMPORT_IDENTITY_KEY,
            IMPORT_MARKER_KEY,
            IMPORT_RECONCILE_BLOCKED_KEY,
            IMPORT_ATTEMPTED_AT_KEY,
            INSTANCE_URL_KEY,
        )
        updates = {key: "" for key in keys}
        overview.update(updates)
        self.repository.update(item.id, AccountUpdateCommand(overview=updates))

    def _mark_imported(self, item: AccountRecord, overview: dict, summary: dict) -> None:
        overview[SYNCED_AT_KEY] = _utcnow().isoformat()
        self.repository.update(
            item.id,
            AccountUpdateCommand(
                lifecycle_status="invalid",
                overview={SYNCED_AT_KEY: overview[SYNCED_AT_KEY]},
            ),
        )
        summary["created"] += 1

    def _save_account_id(self, item: AccountRecord, overview: dict, account_id: int) -> None:
        group_id = str(overview.get(IMPORT_GROUP_ID_KEY) or "")
        overview[ACCOUNT_ID_KEY] = str(account_id)
        overview[SUB2API_GROUP_ID_KEY] = group_id
        overview[IMPORT_GROUP_ID_KEY] = ""
        overview[IMPORT_KNOWN_IDS_KEY] = ""
        overview[IMPORT_FINGERPRINT_KEY] = ""
        overview[IMPORT_IDENTITY_KEY] = ""
        overview[IMPORT_MARKER_KEY] = ""
        overview[IMPORT_RECONCILE_BLOCKED_KEY] = ""
        overview[IMPORT_ATTEMPTED_AT_KEY] = ""
        self.repository.update(
            item.id,
            AccountUpdateCommand(overview={
                ACCOUNT_ID_KEY: overview[ACCOUNT_ID_KEY],
                SUB2API_GROUP_ID_KEY: group_id,
                IMPORT_GROUP_ID_KEY: "",
                IMPORT_KNOWN_IDS_KEY: "",
                IMPORT_FINGERPRINT_KEY: "",
                IMPORT_IDENTITY_KEY: "",
                IMPORT_MARKER_KEY: "",
                IMPORT_RECONCILE_BLOCKED_KEY: "",
                IMPORT_ATTEMPTED_AT_KEY: "",
            }),
        )

    def _push(self, client: Sub2ApiClient, items: list[AccountRecord], group_id: str, instance_url: str) -> dict:
        summary = self._empty_summary(len(items))
        consecutive_failures = 0
        for item in items:
            overview = dict(item.overview or {})
            if overview.get(SYNCED_AT_KEY):
                summary["skipped"] += 1
                if not overview.get(ACCOUNT_ID_KEY):
                    try:
                        known_ids = self._stored_import_ids(overview)
                        if known_ids is not None:
                            if not self._is_same_instance(item, overview, summary, "postprocess_failed", instance_url):
                                continue
                            account_id = client.find_new_account_id(
                                item,
                                known_ids,
                                identity=str(overview.get(IMPORT_IDENTITY_KEY) or item.email),
                                import_marker=str(overview.get(IMPORT_MARKER_KEY) or "") or None,
                            )
                            if account_id is not None:
                                self._save_account_id(item, overview, account_id)
                    except Exception as exc:
                        self._record_error(summary, item, "account_lookup", str(exc))
                    if not overview.get(ACCOUNT_ID_KEY):
                        if not any(error.get("id") == item.id and error.get("stage") in {"account_lookup", "instance_mismatch"} for error in summary["errors"]):
                            self._record_error(summary, item, "account_lookup", "已导入，但无法唯一识别 Sub2API 账号，暂不能后处理")
                        if not any(error.get("id") == item.id and error.get("stage") == "instance_mismatch" for error in summary["errors"]):
                            summary["postprocess_failed"] += 1
                        continue
                group_bound = not overview.get(SUB2API_GROUP_ID_KEY) or overview.get(GROUP_BOUND_AT_KEY)
                if not (group_bound and overview.get(REFRESHED_AT_KEY) and overview.get(MODELS_SYNCED_AT_KEY)):
                    if not self._is_same_instance(item, overview, summary, "postprocess_failed", instance_url):
                        continue
                self._postprocess(client, item, overview, summary)
                continue
            attempt_group_id = str(overview.get(IMPORT_GROUP_ID_KEY) or group_id)
            has_pending_attempt = any(overview.get(key) for key in (IMPORT_GROUP_ID_KEY, IMPORT_KNOWN_IDS_KEY, IMPORT_FINGERPRINT_KEY))
            if has_pending_attempt and not self._is_same_instance(item, overview, summary, "failed", instance_url):
                continue
            if has_pending_attempt:
                attempt_identity = str(overview.get(IMPORT_IDENTITY_KEY) or "")
                attempt_marker = str(overview.get(IMPORT_MARKER_KEY) or "")
                known_ids = self._stored_import_ids(overview)
                if not attempt_identity or not attempt_marker or known_ids is None:
                    summary["failed"] += 1
                    self._record_error(
                        summary,
                        item,
                        "import_reconcile",
                        "待重试导入缺少原始身份或关联标记，无法安全对账；已暂停自动重试",
                    )
                    continue
                try:
                    matching_ids = client.list_matching_account_ids(item, identity=attempt_identity)
                    marked_ids = client.list_matching_account_ids(
                        item,
                        identity=attempt_identity,
                        import_marker=attempt_marker,
                    )
                except Exception as exc:
                    summary["failed"] += 1
                    self._record_error(summary, item, "account_lookup", str(exc))
                    continue
                new_marked_ids = marked_ids - known_ids
                if len(new_marked_ids) == 1:
                    consecutive_failures = 0
                    self._mark_imported(item, overview, summary)
                    self._save_account_id(item, overview, next(iter(new_marked_ids)))
                    self._postprocess(client, item, overview, summary)
                    continue
                if len(new_marked_ids) > 1:
                    summary["failed"] += 1
                    self._record_error(
                        summary,
                        item,
                        "import_reconcile",
                        "待重试导入关联到多个远端账号；为避免操作错误账号，已暂停自动重试",
                    )
                    continue
                unowned_ids = matching_ids - known_ids - marked_ids
                if unowned_ids:
                    summary["failed"] += 1
                    self._record_error(
                        summary,
                        item,
                        "import_reconcile",
                        "待重试期间出现同身份但无本次关联标记的远端账号；为避免误认或重复创建，已暂停自动重试",
                    )
                    continue
                attempted_at = self._attempted_at(overview)
                if attempted_at is not None and (_utcnow() - attempted_at).total_seconds() < IMPORT_RETRY_GRACE_SECONDS:
                    summary["failed"] += 1
                    self._record_error(
                        summary,
                        item,
                        "import_reconcile",
                        f"上次导入请求可能仍在 Sub2API 处理中；{IMPORT_RETRY_GRACE_SECONDS} 秒后再对账",
                    )
                    continue
                # 过了宽限期，远端仍没有带本次标记的新记录：上次没建成。
                # 换新标记（也就换了幂等键），用当前载荷重新导入；旧请求万一迟到落库，会作为无标记记录被上面拦下。
                has_pending_attempt = False
                attempt_group_id = group_id
            if not has_pending_attempt:
                attempt_identity = item.email
                attempt_marker = uuid.uuid4().hex
                known_ids = None
            current_fingerprint = _import_fingerprint(item, attempt_marker)
            try:
                if known_ids is None:
                    known_ids = client.list_matching_account_ids(item, identity=attempt_identity)
                    overview[IMPORT_GROUP_ID_KEY] = attempt_group_id
                    overview[IMPORT_KNOWN_IDS_KEY] = json.dumps(sorted(known_ids))
                    overview[IMPORT_FINGERPRINT_KEY] = current_fingerprint
                    overview[IMPORT_IDENTITY_KEY] = attempt_identity
                    overview[IMPORT_MARKER_KEY] = attempt_marker
                    overview[IMPORT_RECONCILE_BLOCKED_KEY] = ""
                    overview[INSTANCE_URL_KEY] = instance_url
                    self.repository.update(
                        item.id,
                        AccountUpdateCommand(overview={
                            IMPORT_GROUP_ID_KEY: attempt_group_id,
                            IMPORT_KNOWN_IDS_KEY: overview[IMPORT_KNOWN_IDS_KEY],
                            IMPORT_FINGERPRINT_KEY: current_fingerprint,
                            IMPORT_IDENTITY_KEY: attempt_identity,
                            IMPORT_MARKER_KEY: attempt_marker,
                            IMPORT_RECONCILE_BLOCKED_KEY: "",
                            INSTANCE_URL_KEY: overview[INSTANCE_URL_KEY],
                        }),
                    )
            except Exception as exc:
                consecutive_failures += 1
                summary["failed"] += 1
                self._record_error(summary, item, "account_lookup", str(exc))
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    summary["aborted"] = True
                    break
                continue
            overview[IMPORT_ATTEMPTED_AT_KEY] = _utcnow().isoformat()
            self.repository.update(
                item.id,
                AccountUpdateCommand(overview={IMPORT_ATTEMPTED_AT_KEY: overview[IMPORT_ATTEMPTED_AT_KEY]}),
            )
            try:
                ok, message = client.import_account(item, attempt_group_id, attempt_marker)
            except Sub2ApiImportRejected as exc:
                if not has_pending_attempt:
                    self._clear_pending_import(item, overview)
                summary["failed"] += 1
                self._record_error(summary, item, "import", str(exc))
                consecutive_failures = 0
                continue
            if not ok:
                consecutive_failures += 1
                summary["failed"] += 1
                self._record_error(summary, item, "import", message)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    # sub2api 挂了时别让每个账号都各等一次超时。
                    summary["aborted"] = True
                    break
                continue
            consecutive_failures = 0
            self._mark_imported(item, overview, summary)
            try:
                account_id = client.find_new_account_id(
                    item,
                    known_ids,
                    identity=attempt_identity,
                    import_marker=attempt_marker,
                )
            except Exception as exc:
                account_id = None
                self._record_error(summary, item, "account_lookup", str(exc))
            if account_id is None:
                summary["postprocess_failed"] += 1
                if not any(error.get("id") == item.id and error.get("stage") == "account_lookup" for error in summary["errors"]):
                    self._record_error(summary, item, "account_lookup", "导入成功，但无法唯一识别新建的 Sub2API 账号")
                continue
            self._save_account_id(item, overview, account_id)
            self._postprocess(client, item, overview, summary)
        return summary


class Sub2ApiAutoSync:
    def __init__(self, service: Sub2ApiSyncService | None = None):
        self._service = service or Sub2ApiSyncService()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sub2api-auto-sync")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _interval_seconds() -> int:
        raw = str(config_store.get("sub2api_sync_interval_minutes", "") or "").strip()
        try:
            minutes = int(raw) if raw else DEFAULT_INTERVAL_MINUTES
        except ValueError:
            minutes = DEFAULT_INTERVAL_MINUTES
        return max(1, minutes) * 60

    def tick(self, now: float) -> dict | None:
        enabled = str(config_store.get("sub2api_auto_sync", "") or "").strip() == "1"
        has_since = bool(config_store.get(AUTO_SYNC_SINCE_KEY, ""))
        # 刚开启（还没起点）或刚关闭（还有起点）要立刻处理；其余按间隔跑。
        if enabled == has_since and now - self._last_run < self._interval_seconds():
            return None
        self._last_run = now
        return self._service.sync_new_accounts()

    def _loop(self) -> None:
        while not self._stop.wait(AUTO_SYNC_TICK_SECONDS):
            try:
                result = self.tick(time.monotonic())
                if result and any(result[key] for key in ("created", "failed", "group_bind_failed", "refresh_failed", "model_sync_failed", "postprocess_failed")):
                    print(
                        f"[Sub2API] 定时导入: 新增 {result['created']} 导入失败 {result['failed']} "
                        f"分组绑定失败 {result['group_bind_failed']} 刷新失败 {result['refresh_failed']} "
                        f"模型同步失败 {result['model_sync_failed']} 后处理失败 {result['postprocess_failed']}"
                    )
            except Exception as exc:
                print(f"[Sub2API] 定时导入出错: {exc}")


sub2api_auto_sync = Sub2ApiAutoSync()
