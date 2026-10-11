"""Tests for disabled account guards across all subsystems (P7 requirement)."""
from __future__ import annotations

import pytest
from core.base_platform import Account, AccountStatus
from domain.accounts import (
    AccountCreateCommand,
    AccountExportSelection,
    AccountQuery,
    AccountUpdateCommand,
)
from domain.actions import ActionExecutionCommand
from infrastructure.accounts_repository import AccountsRepository
from infrastructure.platform_runtime import PlatformRuntime
from application.tasks import _run_single_account_check
from application.sub2api_sync import Sub2ApiSyncService, Sub2ApiClient


def test_c1_save_account_does_not_overwrite_disabled():
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c1_disabled@example.com",
        password="old_password",
        lifecycle_status="disabled",
    ))
    assert acc.lifecycle_status == "disabled"

    # 外部写入尝试覆盖
    save_account_model = save_account_helper("chatgpt", "c1_disabled@example.com", "new_password")
    reloaded = repo.get(acc.id)
    assert reloaded is not None
    assert reloaded.lifecycle_status == "disabled", f"C1 failure: got {reloaded.lifecycle_status}"


def save_account_helper(platform: str, email: str, password: str):
    from core.db import save_account
    return save_account(Account(
        platform=platform,
        email=email,
        password=password,
        status=AccountStatus.REGISTERED,
    ))


def test_c1_set_disabled_reenable_succeeds():
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c1_reenable@example.com",
        password="pwd",
        lifecycle_status="registered",
    ))
    # 停用
    disabled_acc = repo.set_disabled(acc.id, disabled=True)
    assert disabled_acc is not None
    assert disabled_acc.lifecycle_status == "disabled"

    # 恢复启用
    enabled_acc = repo.set_disabled(acc.id, disabled=False)
    assert enabled_acc is not None
    assert enabled_acc.lifecycle_status == "registered"


def test_c2_sub2api_synced_account_disabled_maintains_disabled_status():
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c2_synced@example.com",
        password="pwd",
        lifecycle_status="disabled",
        overview={"sub2api_synced_at": "2026-10-11T00:00:00Z"},
    ))
    reloaded = repo.get(acc.id)
    assert reloaded is not None
    assert reloaded.lifecycle_status == "disabled"
    assert reloaded.validity_status == "disabled"
    assert reloaded.display_status == "disabled"


def test_c3_disabled_account_with_pending_work_excluded_from_sync_new_accounts(monkeypatch):
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c3_pending@example.com",
        password="pwd",
        lifecycle_status="disabled",
        overview={
            "sub2api_group_id": "group-1",
            "sub2api_instance_url": "http://127.0.0.1:8080",
            "sub2api_synced_at": "2026-10-11T00:00:00Z",
            "sub2api_account_id": "123",
        },
    ))
    service = Sub2ApiSyncService(repository=repo)
    settings = {
        "sub2api_auto_sync": "1",
        "sub2api_auto_sync_since": "2020-01-01T00:00:00+00:00",
        "sub2api_base_url": "http://127.0.0.1:8080",
        "sub2api_admin_key": "dummy",
        "sub2api_default_group_id": "1",
    }
    monkeypatch.setattr("application.sub2api_sync.config_store.get_all", lambda: settings)
    monkeypatch.setattr(service, "_client", lambda s: None)

    pushed_items = []
    monkeypatch.setattr(service, "_push", lambda client, items, group_id, instance_url: pushed_items.extend(items) or {})
    service.sync_new_accounts()

    assert not any(item.id == acc.id for item in pushed_items), "Disabled account was erroneously included in sync_new_accounts"


def test_c4_run_single_account_check_skips_disabled(monkeypatch):
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c4_check@example.com",
        password="pwd",
        lifecycle_status="disabled",
    ))
    called = []
    from platforms.chatgpt.plugin import ChatGPTPlatform
    monkeypatch.setattr(ChatGPTPlatform, "check_valid", lambda self, account: called.append(True) or True)

    valid, result = _run_single_account_check(acc.id)
    assert valid is False
    assert result.get("skipped") is True
    assert result.get("reason") == "disabled"
    assert len(called) == 0


def test_c5_patch_account_lifecycle_disabled_rejected(client):
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c5_patch@example.com",
        password="pwd",
        lifecycle_status="disabled",
    ))
    # 尝试 PATCH 停用账号
    resp = client.patch(f"/api/accounts/{acc.id}", json={"lifecycle_status": "registered"})
    assert resp.status_code == 409

    # 尝试通过 PATCH 将普通账号置为 disabled
    acc2 = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c5_patch2@example.com",
        password="pwd",
        lifecycle_status="registered",
    ))
    resp2 = client.patch(f"/api/accounts/{acc2.id}", json={"lifecycle_status": "disabled"})
    assert resp2.status_code == 409


def test_c6_export_csv_without_status_excludes_disabled():
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c6_csv_disabled@example.com",
        password="pwd",
        lifecycle_status="disabled",
    ))
    csv_text = repo.export_csv(AccountQuery(platform="chatgpt", status="", page=1, page_size=100))
    assert "c6_csv_disabled@example.com" not in csv_text


def test_c7_platform_runtime_execute_action_guards_disabled(monkeypatch):
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c7_action@example.com",
        password="pwd",
        lifecycle_status="disabled",
    ))
    runtime = PlatformRuntime()
    result = runtime.execute_action(ActionExecutionCommand(
        platform="chatgpt",
        account_id=acc.id,
        action_id="generate_auth_url",
        params={},
    ))
    assert result.ok is False
    assert "账号已停用" in (result.error or "")


def test_p4_select_for_export_and_sync_selected_exclude_disabled():
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="p4_export@example.com",
        password="pwd",
        lifecycle_status="disabled",
    ))
    # select_all 排除 disabled
    records = repo.select_for_export(AccountExportSelection(platform="chatgpt", select_all=True))
    assert not any(r.id == acc.id for r in records)

    # 显式 ids 保留
    specific = repo.select_for_export(AccountExportSelection(platform="chatgpt", ids=[acc.id]))
    assert any(r.id == acc.id for r in specific)


def test_d1_import_lines_skips_disabled_account():
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="d1_import@example.com",
        password="pwd",
        lifecycle_status="disabled",
    ))
    from domain.accounts import AccountImportLine
    created, skipped = repo.import_lines("chatgpt", [AccountImportLine(email="d1_import@example.com", password="newpwd")])
    assert created == 0
    assert skipped == 1


def test_c3_outer_filter_blocks_disabled_pending_work_even_if_selection_returns_it(monkeypatch):
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="c3_outer_filter@example.com",
        password="pwd",
        lifecycle_status="disabled",
        overview={
            "sub2api_synced_at": "2026-10-11T00:00:00Z",
            "sub2api_account_id": "123",
        },
    ))
    record = repo.get(acc.id)
    assert record.lifecycle_status == "disabled"
    assert Sub2ApiSyncService._has_pending_sub2api_work(record)
    service = Sub2ApiSyncService(repository=repo)
    settings = {
        "sub2api_auto_sync": "1",
        "sub2api_auto_sync_since": "2020-01-01T00:00:00+00:00",
        "sub2api_base_url": "http://127.0.0.1:8080",
        "sub2api_admin_key": "dummy",
        "sub2api_default_group_id": "1",
    }
    monkeypatch.setattr("application.sub2api_sync.config_store.get_all", lambda: settings)
    monkeypatch.setattr(service, "_client", lambda s: None)
    # 绕过 select_for_export 的全选过滤，只让 sync_new_accounts 自己那道过滤起作用
    monkeypatch.setattr(repo, "select_for_export", lambda selection, include_failed=False: [record])
    pushed_items = []
    monkeypatch.setattr(service, "_push", lambda client, items, group_id, instance_url: pushed_items.extend(items) or {})
    service.sync_new_accounts()
    assert not any(item.id == acc.id for item in pushed_items)


def test_p4_sync_selected_skips_disabled_and_counts_it(monkeypatch):
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(
        platform="chatgpt",
        email="p4_sync_selected@example.com",
        password="pwd",
        lifecycle_status="disabled",
    ))
    service = Sub2ApiSyncService(repository=repo)
    settings = {
        "sub2api_base_url": "http://127.0.0.1:8080",
        "sub2api_admin_key": "dummy",
        "sub2api_default_group_id": "1",
    }
    monkeypatch.setattr("application.sub2api_sync.config_store.get_all", lambda: settings)
    monkeypatch.setattr(service, "_client", lambda s: None)
    pushed_items = []

    def fake_push(client, items, group_id, instance_url):
        pushed_items.extend(items)
        return {"skipped": 0}

    monkeypatch.setattr(service, "_push", fake_push)
    result = service.sync_selected(AccountExportSelection(platform="chatgpt", ids=[acc.id]))
    assert not any(item.id == acc.id for item in pushed_items)
    assert result["skipped"] == 1
