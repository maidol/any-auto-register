from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

from application import tasks
from core import lifecycle


def _account_fixture(monkeypatch):
    account = SimpleNamespace(id=17, platform="chatgpt", email="imported@test.com")
    session = MagicMock()
    session.__enter__.return_value = session
    session.exec.return_value.all.return_value = [account]
    graph = {
        "lifecycle_status": "registered",
        "overview": {"sub2api_synced_at": "2026-09-29T00:00:00+00:00"},
        "credentials": [
            {"scope": "platform", "key": "refresh_token", "value": "refresh-token"},
            {"scope": "platform", "key": "session_token", "value": "session-token"},
        ],
    }
    monkeypatch.setattr(lifecycle, "Session", lambda _engine: session)
    monkeypatch.setattr(lifecycle, "load_account_graphs", lambda _session, _ids: {17: graph})
    return account


def test_validity_check_pages_past_newer_imported_accounts(monkeypatch):
    imported = SimpleNamespace(id=17, platform="chatgpt", email="imported@test.com")
    active = SimpleNamespace(id=18, platform="chatgpt", email="active@test.com")
    session = MagicMock()
    session.__enter__.return_value = session
    session.exec.side_effect = [
        SimpleNamespace(all=MagicMock(return_value=[imported])),
        SimpleNamespace(all=MagicMock(return_value=[active])),
    ]
    session.get.return_value = active
    graphs = {
        17: {"lifecycle_status": "invalid", "overview": {"sub2api_synced_at": "t"}},
        18: {"lifecycle_status": "registered", "overview": {}},
    }
    plugin = MagicMock()
    plugin.check_valid.return_value = True
    monkeypatch.setattr(lifecycle, "Session", lambda _engine: session)
    monkeypatch.setattr(lifecycle, "load_account_graphs", lambda _session, ids: {id_: graphs[id_] for id_ in ids})
    monkeypatch.setattr(lifecycle, "get", lambda _platform: lambda **_kwargs: plugin)
    monkeypatch.setattr(lifecycle, "build_platform_account", MagicMock())
    monkeypatch.setattr(lifecycle, "patch_account_graph", MagicMock())

    result = lifecycle.check_accounts_validity(platform="chatgpt", limit=1)

    assert result["valid"] == 1
    assert result["skipped"] == 1
    plugin.check_valid.assert_called_once()


def test_sub2api_imported_account_is_excluded_from_validity_check(monkeypatch):
    _account_fixture(monkeypatch)
    plugin = MagicMock()
    monkeypatch.setattr(lifecycle, "get", lambda _platform: lambda **_kwargs: plugin)

    result = lifecycle.check_accounts_validity(platform="chatgpt")

    assert result["skipped"] == 1
    plugin.check_valid.assert_not_called()


def test_sub2api_imported_account_is_excluded_from_token_refresh(monkeypatch):
    _account_fixture(monkeypatch)
    fake_manager = MagicMock()
    token_refresh = ModuleType("platforms.chatgpt.token_refresh")
    token_refresh.TokenRefreshManager = MagicMock(return_value=fake_manager)
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.token_refresh", token_refresh)

    result = lifecycle.refresh_expiring_tokens(platform="chatgpt")

    assert result["skipped"] == 1
    assert result["refreshed"] == 0
    fake_manager.refresh_account.assert_not_called()


def test_check_all_pages_past_newer_imported_accounts(monkeypatch):
    imported = SimpleNamespace(id=17, platform="chatgpt", email="imported@test.com")
    active = SimpleNamespace(id=18, platform="chatgpt", email="active@test.com")
    session = MagicMock()
    session.__enter__.return_value = session
    session.exec.side_effect = [
        SimpleNamespace(all=MagicMock(return_value=[imported])),
        SimpleNamespace(all=MagicMock(return_value=[active])),
    ]
    session.get.return_value = active
    graphs = {
        17: {"lifecycle_status": "invalid", "overview": {"sub2api_synced_at": "t"}},
        18: {"lifecycle_status": "registered", "overview": {}},
    }
    monkeypatch.setattr(tasks, "Session", lambda _engine: session)
    monkeypatch.setattr(tasks, "load_account_graphs", lambda _session, ids: {id_: graphs[id_] for id_ in ids})
    monkeypatch.setattr(tasks, "build_platform_account", MagicMock())
    monkeypatch.setattr(tasks, "patch_account_graph", MagicMock())

    class Plugin:
        def check_valid(self, _account):
            return True

    plugin = Plugin()
    monkeypatch.setattr(tasks, "get", lambda _platform: lambda **_kwargs: plugin)
    logger = MagicMock()
    logger.is_cancel_requested.return_value = False

    tasks._execute_account_check_all_task({"platform": "chatgpt", "limit": 1}, logger)

    assert logger.set_result_data.call_args.args[0]["valid"] == 1


def test_sub2api_imported_account_is_not_manually_checked(monkeypatch):
    model = SimpleNamespace(id=17, platform="chatgpt", email="imported@test.com")
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = model
    graph = {"overview": {"sub2api_synced_at": "2026-09-29T00:00:00+00:00"}}
    monkeypatch.setattr(tasks, "Session", lambda _engine: session)
    monkeypatch.setattr(tasks, "load_account_graphs", lambda _session, _ids: {17: graph})
    monkeypatch.setattr(tasks, "build_platform_account", MagicMock())
    plugin = MagicMock()
    monkeypatch.setattr(tasks, "get", lambda _platform: lambda **_kwargs: plugin)

    _valid, result = tasks._run_single_account_check(17)

    plugin.check_valid.assert_not_called()
    assert result["skipped"] is True


def test_sub2api_imported_account_is_excluded_from_cpa_refresh(monkeypatch):
    _account_fixture(monkeypatch)
    fake_requests = MagicMock()
    curl_cffi = ModuleType("curl_cffi")
    curl_cffi.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", curl_cffi)

    result = lifecycle.refresh_and_sync_cpa()

    assert result["skipped"] == 1
    assert result["refreshed"] == 0
    fake_requests.Session.assert_not_called()
