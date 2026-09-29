"""已导入 Sub2API 的账号在本系统作废：列表标明、禁止本地刷新 Token。"""
from __future__ import annotations

import sys
from types import ModuleType

from domain.accounts import AccountCreateCommand, AccountUpdateCommand
from domain.actions import ActionExecutionCommand
from infrastructure.accounts_repository import AccountsRepository
from infrastructure.platform_runtime import PlatformRuntime


class _FakeRefreshResult:
    success = True
    access_token = "at_new"
    refresh_token = "rt_new"
    error_message = ""


class _FakeRefreshManager:
    calls = 0

    def __init__(self, proxy_url=None):
        pass

    def refresh_account(self, account):
        _FakeRefreshManager.calls += 1
        return _FakeRefreshResult()


def _create(email: str, *, imported: bool) -> int:
    record = AccountsRepository().create(
        AccountCreateCommand(
            platform="chatgpt",
            email=email,
            password="pw",
            credentials={"access_token": "at_old", "refresh_token": "rt_old"},
        )
    )
    if imported:
        AccountsRepository().update(
            record.id,
            AccountUpdateCommand(overview={"sub2api_synced_at": "2026-09-29T00:00:00+00:00"}),
        )
    return record.id


def _refresh(monkeypatch, account_id: int):
    from platforms.chatgpt import switch

    # 真的 platforms.chatgpt.token_refresh 在 import 时就会 NameError（注解里的 Account 没有导入），
    # 所以换成只带 TokenRefreshManager 的假模块；不要去修那个模块，它不在这次范围内。
    token_refresh = ModuleType("platforms.chatgpt.token_refresh")
    token_refresh.TokenRefreshManager = _FakeRefreshManager
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.token_refresh", token_refresh)
    _FakeRefreshManager.calls = 0
    monkeypatch.setattr(switch, "fetch_chatgpt_account_state", lambda **_kwargs: {})
    return PlatformRuntime().execute_action(
        ActionExecutionCommand(platform="chatgpt", account_id=account_id, action_id="refresh_token", params={})
    )


def test_refresh_token_action_is_refused_for_sub2api_imported_account(monkeypatch):
    account_id = _create("imported@test.com", imported=True)

    result = _refresh(monkeypatch, account_id)

    assert result.ok is False
    assert "Sub2API" in result.error
    assert _FakeRefreshManager.calls == 0


def test_refresh_token_action_still_runs_for_local_account(monkeypatch):
    # 反向对照：守卫只拦已导入的账号。这条在修复前后都应该是绿的。
    account_id = _create("local@test.com", imported=False)

    result = _refresh(monkeypatch, account_id)

    assert result.ok is True, result.error
    assert _FakeRefreshManager.calls == 1


def test_account_list_marks_sub2api_imported_account_unavailable():
    imported_id = _create("shown@test.com", imported=True)
    local_id = _create("plain@test.com", imported=False)

    imported = AccountsRepository().get(imported_id)
    local = AccountsRepository().get(local_id)

    assert imported.display_status == "invalid"
    assert imported.display_summary["badges"][0]["label"] == "已导入 Sub2API · 不可用"
    assert any(warning["key"] == "sub2api_imported" for warning in imported.display_summary["warnings"])
    assert all("Sub2API" not in badge["label"] for badge in local.display_summary["badges"])
