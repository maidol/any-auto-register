"""Sub2API 手动 / 定时导入。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from application import sub2api_sync as s2a
from core.config_store import config_store
from domain.accounts import AccountCreateCommand, AccountExportSelection
from infrastructure.accounts_repository import AccountsRepository


def _create(email: str, *, access_token: str = "at_x", refresh_token: str = "rt_x") -> int:
    record = AccountsRepository().create(
        AccountCreateCommand(
            platform="chatgpt",
            email=email,
            password="pw",
            credentials={"access_token": access_token, "refresh_token": refresh_token},
        )
    )
    return record.id


def _configure(**extra: str) -> None:
    config_store.set_many({"sub2api_url": "http://s2a.local/", "sub2api_admin_key": "admin-key", **extra})


class FakeClient:
    def __init__(self, results: list[tuple[bool, str]] | None = None):
        self.results = list(results or [])
        self.pushed: list[str] = []

    def import_account(self, item):
        self.pushed.append(item.email)
        return self.results.pop(0) if self.results else (True, "")


def _service(client: FakeClient) -> s2a.Sub2ApiSyncService:
    return s2a.Sub2ApiSyncService(client_factory=lambda _url, _key: client)


def _response(status: int, body) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = str(body)
    return resp


# ── 客户端 ───────────────────────────────────────────────────


def test_client_posts_wrapped_payload_with_admin_key_and_idempotency_key():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    ok_body = {"code": 0, "message": "success", "data": {"account_created": 1, "account_failed": 0}}

    with patch("application.sub2api_sync.requests.post", return_value=_response(200, ok_body)) as post:
        assert s2a.Sub2ApiClient("http://s2a.local/", "admin-key").import_account(item) == (True, "")

    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "http://s2a.local/api/v1/admin/accounts/data"
    assert kwargs["headers"]["x-api-key"] == "admin-key"
    assert kwargs["headers"]["Idempotency-Key"].startswith("aar-sub2api-")
    assert kwargs["json"]["skip_default_group_bind"] is True
    assert kwargs["json"]["data"]["accounts"][0]["name"] == "a@test.com"
    assert kwargs["json"]["data"]["proxies"] == []


def test_client_idempotency_key_is_stable_for_same_account():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    data = s2a._make_sub2api_json(item)
    assert s2a._idempotency_key(item, data) == s2a._idempotency_key(item, data)
    assert len(s2a._idempotency_key(item, data)) <= 128


def test_client_treats_http200_without_created_account_as_failure():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    body = {
        "code": 0,
        "message": "success",
        "data": {"account_created": 0, "account_failed": 1, "errors": [{"kind": "account", "message": "bad credentials"}]},
    }

    with patch("application.sub2api_sync.requests.post", return_value=_response(200, body)):
        assert s2a.Sub2ApiClient("http://s2a.local", "k").import_account(item) == (False, "bad credentials")


def test_client_reports_http_error_message():
    account_id = _create("a@test.com")
    item = AccountsRepository().get(account_id)
    with patch("application.sub2api_sync.requests.post", return_value=_response(401, {"code": 401, "message": "invalid admin api key"})):
        ok, message = s2a.Sub2ApiClient("http://s2a.local", "k").import_account(item)
    assert ok is False
    assert "401" in message and "invalid admin api key" in message


def test_client_skips_account_without_tokens_without_request():
    account_id = _create("empty@test.com", access_token="", refresh_token="")
    item = AccountsRepository().get(account_id)
    with patch("application.sub2api_sync.requests.post") as post:
        ok, _message = s2a.Sub2ApiClient("http://s2a.local", "k").import_account(item)
    assert ok is False
    post.assert_not_called()


# ── 手动导入 ─────────────────────────────────────────────────


def test_manual_sync_marks_success_and_never_pushes_twice():
    _configure()
    account_id = _create("a@test.com")
    client = FakeClient()
    service = _service(client)
    selection = lambda: AccountExportSelection(platform="chatgpt", ids=[account_id])  # noqa: E731

    first = service.sync_selected(selection())
    second = service.sync_selected(selection())

    assert client.pushed == ["a@test.com"]
    assert first["created"] == 1 and first["skipped"] == 0
    assert second["created"] == 0 and second["skipped"] == 1
    assert AccountsRepository().get(account_id).overview.get(s2a.SYNCED_AT_KEY)


def test_manual_sync_failure_is_not_marked_and_retries_next_time():
    _configure()
    account_id = _create("a@test.com")
    client = FakeClient([(False, "boom")])
    service = _service(client)

    first = service.sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))
    second = service.sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))

    assert first["failed"] == 1 and first["errors"][0]["message"] == "boom"
    assert second["created"] == 1
    assert client.pushed == ["a@test.com", "a@test.com"]


def test_manual_sync_rejects_empty_selection_instead_of_pushing_everything():
    _configure()
    _create("a@test.com")
    _create("b@test.com")
    client = FakeClient()

    with pytest.raises(ValueError, match="勾选"):
        _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[]))
    assert client.pushed == []


def test_manual_sync_requires_config():
    account_id = _create("a@test.com")
    client = FakeClient()
    with pytest.raises(ValueError, match="Sub2API"):
        _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=[account_id]))
    assert client.pushed == []


def test_manual_sync_stops_after_consecutive_failures():
    _configure()
    ids = [_create(f"u{i}@test.com") for i in range(5)]
    client = FakeClient([(False, "down")] * 5)

    result = _service(client).sync_selected(AccountExportSelection(platform="chatgpt", ids=ids))

    assert result["aborted"] is True
    assert result["failed"] == s2a.MAX_CONSECUTIVE_FAILURES
    assert len(client.pushed) == s2a.MAX_CONSECUTIVE_FAILURES


def test_sync_endpoint_rejects_empty_selection(client):
    _configure()
    resp = client.post("/api/accounts/sync/sub2api", json={"platform": "chatgpt", "ids": []})
    assert resp.status_code == 400


# ── 定时导入 ─────────────────────────────────────────────────


def test_auto_sync_disabled_does_nothing():
    _configure()
    _create("a@test.com")
    client = FakeClient()
    assert _service(client).sync_new_accounts() is None
    assert client.pushed == []


def test_auto_sync_only_pushes_accounts_created_after_enable():
    _configure(sub2api_auto_sync="1")
    _create("old@test.com")
    client = FakeClient()
    service = _service(client)

    first = service.sync_new_accounts()
    assert first["total"] == 0
    assert config_store.get(s2a.AUTO_SYNC_SINCE_KEY)
    assert client.pushed == []

    _create("new@test.com")
    second = service.sync_new_accounts()
    third = service.sync_new_accounts()

    assert client.pushed == ["new@test.com"]
    assert second["created"] == 1
    assert third["created"] == 0 and third["skipped"] == 1


def test_auto_sync_disable_clears_start_point():
    _configure(sub2api_auto_sync="1")
    service = _service(FakeClient())
    service.sync_new_accounts()
    assert config_store.get(s2a.AUTO_SYNC_SINCE_KEY)

    config_store.set("sub2api_auto_sync", "0")
    assert service.sync_new_accounts() is None
    assert config_store.get(s2a.AUTO_SYNC_SINCE_KEY) == ""


def test_auto_sync_tick_respects_interval():
    _configure(sub2api_auto_sync="1", sub2api_sync_interval_minutes="10")
    calls = []

    class FakeService:
        def sync_new_accounts(self):
            calls.append(1)
            if not config_store.get(s2a.AUTO_SYNC_SINCE_KEY):
                config_store.set(s2a.AUTO_SYNC_SINCE_KEY, "2026-01-01T00:00:00+00:00")
            return None

    runner = s2a.Sub2ApiAutoSync(FakeService())
    runner.tick(1000.0)          # 刚开启：立刻记起点
    runner.tick(1060.0)          # 间隔没到
    runner.tick(1000.0 + 600)    # 到了
    assert len(calls) == 2
