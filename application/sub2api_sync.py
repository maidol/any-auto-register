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
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from application.account_exports import CHATGPT_PLATFORM, _make_sub2api_json
from core.config_store import config_store
from domain.accounts import AccountExportSelection, AccountRecord, AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository

SYNCED_AT_KEY = "sub2api_synced_at"
AUTO_SYNC_SINCE_KEY = "sub2api_auto_sync_since"
AUTO_SYNC_STATUSES = {"registered", "trial", "subscribed"}
DEFAULT_INTERVAL_MINUTES = 10
REQUEST_TIMEOUT_SECONDS = 20
MAX_CONSECUTIVE_FAILURES = 3
AUTO_SYNC_TICK_SECONDS = 60

# 手动和定时可能同时跑；不串行的话同一个账号会在两边各推一次，而 sub2api 不去重。
_SYNC_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    # SQLite 读回来的是 naive datetime，库里存的是 UTC。
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _idempotency_key(item: AccountRecord, data: dict) -> str:
    # 同一账号同一份凭据 → 同一个 key：超时后重推会被 sub2api 回放，而不是再建一条。
    raw = f"{item.id}:{json.dumps(data, sort_keys=True, ensure_ascii=False)}"
    return "aar-sub2api-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def _has_token(data: dict) -> bool:
    accounts = data.get("accounts") or [{}]
    credentials = accounts[0].get("credentials") or {}
    return bool(credentials.get("access_token") or credentials.get("refresh_token"))


class Sub2ApiClient:
    def __init__(self, base_url: str, admin_key: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS):
        self.base_url = base_url.strip().rstrip("/")
        self.admin_key = admin_key.strip()
        self.timeout = timeout

    def import_account(self, item: AccountRecord) -> tuple[bool, str]:
        data = _make_sub2api_json(item)
        if not _has_token(data):
            return False, "账号缺少 access_token / refresh_token"
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/admin/accounts/data",
                json={"data": data, "skip_default_group_bind": True},
                headers={
                    "x-api-key": self.admin_key,
                    "Idempotency-Key": _idempotency_key(item, data),
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
        if resp.status_code != 200 or body.get("code") != 0:
            return False, f"HTTP {resp.status_code}: {body.get('message') or resp.text[:200]}"
        # HTTP 200 不等于建成了：校验失败的账号只进 errors，account_created 是 0。
        result = body.get("data") or {}
        if int(result.get("account_created") or 0) < 1:
            errors = result.get("errors") or []
            message = (errors[0] or {}).get("message") if errors else ""
            return False, str(message or "sub2api 未创建账号")
        return True, ""


class Sub2ApiSyncService:
    def __init__(
        self,
        repository: AccountsRepository | None = None,
        client_factory: Callable[[str, str], Sub2ApiClient] | None = None,
    ):
        self.repository = repository or AccountsRepository()
        self._client_factory = client_factory or Sub2ApiClient

    def _client(self) -> Sub2ApiClient:
        base_url = str(config_store.get("sub2api_url", "") or "").strip()
        admin_key = str(config_store.get("sub2api_admin_key", "") or "").strip()
        if not base_url or not admin_key:
            raise ValueError("未配置 Sub2API 地址或 Admin API Key，请先到 设置 → ChatGPT → Sub2API 填写")
        return self._client_factory(base_url, admin_key)

    def sync_selected(self, selection: AccountExportSelection) -> dict:
        if not selection.select_all and not selection.ids:
            # select_for_export 在 ids 为空时返回全部账号：导出无害，这里会把整库推过去。
            raise ValueError("请先勾选要导入的账号")
        selection.platform = selection.platform or CHATGPT_PLATFORM
        if selection.platform != CHATGPT_PLATFORM:
            raise ValueError("仅支持 ChatGPT 账号导入 Sub2API")
        client = self._client()
        with _SYNC_LOCK:
            items = self.repository.select_for_export(selection)
            return self._push(client, items)

    def sync_new_accounts(self) -> dict | None:
        """定时入口。未开启返回 None；第一次看到开启只记起点、不推。"""
        if str(config_store.get("sub2api_auto_sync", "") or "").strip() != "1":
            if config_store.get(AUTO_SYNC_SINCE_KEY, ""):
                # 关掉时清起点：下次开启只认那之后新增的，不把关闭期间的补推上去。
                config_store.set(AUTO_SYNC_SINCE_KEY, "")
            return None
        since_text = str(config_store.get(AUTO_SYNC_SINCE_KEY, "") or "").strip()
        if not since_text:
            # 存量账号走手动导入；定时只管开启之后新增的。
            config_store.set(AUTO_SYNC_SINCE_KEY, _utcnow().isoformat())
            return {"total": 0, "created": 0, "skipped": 0, "failed": 0, "aborted": False, "errors": []}
        since = _as_utc(datetime.fromisoformat(since_text))
        client = self._client()
        with _SYNC_LOCK:
            items = self.repository.select_for_export(
                AccountExportSelection(platform=CHATGPT_PLATFORM, select_all=True)
            )
            items = [
                item for item in items
                if item.lifecycle_status in AUTO_SYNC_STATUSES
                and (_as_utc(item.created_at) or since) >= since
            ]
            return self._push(client, items)

    def _push(self, client: Sub2ApiClient, items: list[AccountRecord]) -> dict:
        summary = {"total": len(items), "created": 0, "skipped": 0, "failed": 0, "aborted": False, "errors": []}
        consecutive_failures = 0
        for item in items:
            if (item.overview or {}).get(SYNCED_AT_KEY):
                summary["skipped"] += 1
                continue
            ok, message = client.import_account(item)
            if ok:
                consecutive_failures = 0
                self.repository.update(
                    item.id,
                    AccountUpdateCommand(overview={SYNCED_AT_KEY: _utcnow().isoformat()}),
                )
                summary["created"] += 1
                continue
            consecutive_failures += 1
            summary["failed"] += 1
            summary["errors"].append({"id": item.id, "email": item.email, "message": message})
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # sub2api 挂了时别让每个账号都各等一次超时。
                summary["aborted"] = True
                break
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
                if result and (result["created"] or result["failed"]):
                    print(f"[Sub2API] 定时导入: 新增 {result['created']} 失败 {result['failed']}")
            except Exception as exc:
                print(f"[Sub2API] 定时导入出错: {exc}")


sub2api_auto_sync = Sub2ApiAutoSync()
