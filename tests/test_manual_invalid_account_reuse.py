"""Manual-origin invalid account email reuse regression tests."""
from __future__ import annotations

import pytest

from domain.accounts import AccountCreateCommand
from infrastructure.accounts_repository import AccountsRepository
from core import db as db_module


def _manual_row(
    email: str,
    *,
    password: str = "ManualPw123!",
    platform: str = "chatgpt",
    lifecycle_status: str = "registered",
    account_origin: str | None = "manual",
    valid: bool | None = False,
) -> None:
    overview = {}
    if account_origin is not None:
        overview["account_origin"] = account_origin
    if valid is not None:
        overview["valid"] = valid
    AccountsRepository().create(
        AccountCreateCommand(
            platform=platform,
            email=email,
            password=password,
            lifecycle_status=lifecycle_status,
            overview=overview,
        )
    )


def _overview(email: str, platform: str = "chatgpt") -> dict:
    from sqlmodel import Session, select
    from core.account_graph import load_account_graphs

    with Session(db_module.engine) as session:
        model = session.exec(
            select(db_module.AccountModel)
            .where(db_module.AccountModel.platform == platform)
            .where(db_module.AccountModel.email == email)
        ).first()
        graph = load_account_graphs(session, [int(model.id)]).get(int(model.id), {})
    return {**(graph.get("overview") or {}), "lifecycle_status": graph.get("lifecycle_status"),
            "validity_status": graph.get("validity_status"),
            "display_status": graph.get("display_status")}


def test_manual_invalid_candidates_require_manual_origin_invalid_status_and_password():
    _manual_row("eligible@example.test")
    _manual_row("valid@example.test", valid=True)
    _manual_row("unknown@example.test", valid=None)
    _manual_row("legacy@example.test", account_origin=None)
    _manual_row("nopw@example.test", password="")
    _manual_row("failed@example.test", lifecycle_status="failed")
    _manual_row("other-platform@example.test", platform="cursor")

    rows = db_module.list_reusable_manual_invalid_accounts("chatgpt")

    assert [row["email"] for row in rows] == ["eligible@example.test"]
    assert rows[0]["password"] == "ManualPw123!"


def test_successful_account_save_preserves_manual_origin():
    from core.base_platform import Account

    _manual_row("saved@example.test")
    db_module.save_account(Account(
        platform="chatgpt",
        email="saved@example.test",
        password="ManualPw123!",
        extra={"account_overview": {"valid": False}},
    ))

    overview = _overview("saved@example.test")
    assert overview["account_origin"] == "manual"
    assert overview["validity_status"] == "invalid"


def test_manual_reuse_count_uses_a_separate_key_and_preserves_account_status():
    _manual_row("count@example.test")

    for _ in range(db_module.FAILED_ACCOUNT_REUSE_MAX):
        db_module.mark_manual_invalid_account_reused("chatgpt", "count@example.test")

    overview = _overview("count@example.test")
    assert overview["manual_reuse_attempts"] == db_module.FAILED_ACCOUNT_REUSE_MAX
    assert overview["lifecycle_status"] == "registered"
    assert overview["validity_status"] == "invalid"
    assert overview["account_origin"] == "manual"
    assert "reuse_attempts" not in overview
    assert db_module.list_reusable_manual_invalid_accounts("chatgpt") == []


def _manual_world(monkeypatch, *, outcomes):
    from tests.test_failed_account_reuse import _world

    world = _world(monkeypatch, outcomes=outcomes)
    world.mailbox.ensure_calls = []

    def ensure_address(email):
        from core.base_mailbox import MailboxAccount

        world.mailbox.ensure_calls.append(email)
        world.mailbox.ensure_result = MailboxAccount(email=email, account_id="temporary-token", extra={
            "provider_resource": {
                "provider_type": "mailbox",
                "provider_name": "moemail",
                "resource_type": "mailbox",
                "resource_identifier": "temporary-token",
                "handle": email,
                "display_name": email,
                "metadata": {"email": email},
            },
        })
        return world.mailbox.ensure_result

    world.mailbox.ensure_address = ensure_address
    return world


def test_manual_invalid_email_is_opt_in_and_preserves_failed_account_path(monkeypatch):
    _manual_row("not-opted-in@example.test")
    world = _manual_world(monkeypatch, outcomes=["ok"])

    world.run(count=1, retry_count=0, extra={"reuse_manual_invalid_emails": False})

    assert world.mailbox.ensure_calls == []
    assert world.emails == ["box1@example.com"]


def test_opted_in_manual_invalid_email_uses_service_account_and_stored_password(monkeypatch):
    import application.tasks as tasks_module

    _manual_row("manual@example.test")
    world = _manual_world(monkeypatch, outcomes=["ok"])
    original_build = tasks_module._build_platform_instance
    pinned_accounts = []

    def capture_pinned_account(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
        if shared_mailbox is not None:
            pinned_accounts.append(shared_mailbox.get_email())
        return original_build(platform_name, payload, logger, resolved_proxy, shared_mailbox)

    monkeypatch.setattr(tasks_module, "_build_platform_instance", capture_pinned_account)
    world.run(count=1, retry_count=0, extra={"reuse_manual_invalid_emails": True})

    assert world.mailbox.ensure_calls == ["manual@example.test"]
    assert world.emails == ["manual@example.test"]
    assert world.passwords == ["ManualPw123!"]
    assert world.mailbox.n == 0
    assert pinned_accounts[0] is world.mailbox.ensure_result
    overview = _overview("manual@example.test")
    assert overview["account_origin"] == "manual"
    assert overview["manual_reuse_attempts"] == 1


def test_existing_failed_account_remains_ahead_of_manual_candidate(monkeypatch):
    from tests.test_failed_account_reuse import _failed

    _failed("existing@reuse.test", "not_created")
    _manual_row("manual-priority@example.test")
    world = _manual_world(monkeypatch, outcomes=["ok"])

    world.run(count=1, retry_count=0, extra={"reuse_manual_invalid_emails": True})

    assert world.emails == ["existing@reuse.test"]
    assert world.mailbox.ensure_calls == ["existing@reuse.test"]


def test_manual_reuse_calls_only_selected_provider_when_mailbox_is_fallback(monkeypatch):
    from core.base_mailbox import FallbackMailbox

    _manual_row("fallback@example.test")
    world = _manual_world(monkeypatch, outcomes=["ok"])
    other_calls = []

    class OtherMailbox:
        def ensure_address(self, email):
            other_calls.append(email)
            raise AssertionError("non-selected provider must not be called")

    fallback = FallbackMailbox([("moemail", world.mailbox), ("other", OtherMailbox())])
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **_kw: fallback)

    world.run(count=1, retry_count=0, extra={
        "mail_provider": "moemail",
        "reuse_manual_invalid_emails": True,
    })

    assert world.mailbox.ensure_calls == ["fallback@example.test"]
    assert other_calls == []
    assert world.emails == ["fallback@example.test"]
    assert world.mailbox.ensure_result.extra["mailbox_provider_key"] == "moemail"


def test_failed_manual_registration_preserves_status_and_does_not_enter_failed_pool(monkeypatch):
    _manual_row("manual-failure@example.test")
    world = _manual_world(monkeypatch, outcomes=["boom"])

    world.run(count=1, retry_count=0, max_attempts=1,
              extra={"reuse_manual_invalid_emails": True})

    overview = _overview("manual-failure@example.test")
    assert overview["lifecycle_status"] == "registered"
    assert overview["validity_status"] == "invalid"
    assert overview["account_origin"] == "manual"
    failed_emails = {row["email"] for row in db_module.list_reusable_failed_accounts("chatgpt")}
    assert "manual-failure@example.test" not in failed_emails


def test_failed_lifecycle_manual_row_is_not_returned_twice():
    _manual_row("failed-manual@example.test", lifecycle_status="failed")

    assert db_module.list_reusable_manual_invalid_accounts("chatgpt") == []


def test_manual_address_provider_error_does_not_consume_candidate(monkeypatch):
    _manual_row("provider-error@example.test")
    world = _manual_world(monkeypatch, outcomes=["ok"])
    world.mailbox.ensure_address = lambda _email: (_ for _ in ()).throw(RuntimeError("unavailable"))

    world.run(count=1, retry_count=0, extra={"reuse_manual_invalid_emails": True})

    overview = _overview("provider-error@example.test")
    assert world.emails == ["box1@example.com"]
    assert "manual_reuse_attempts" not in overview
    assert overview["lifecycle_status"] == "registered"
    assert overview["validity_status"] == "invalid"


def test_manual_reuse_is_skipped_for_explicit_email_and_other_platform(monkeypatch):
    _manual_row("not-selected@example.test")
    world = _manual_world(monkeypatch, outcomes=["ok", "ok"])
    flag = {"reuse_manual_invalid_emails": True}

    world.run(count=1, retry_count=0, email="fixed@example.test", extra=flag)
    world.run(count=1, retry_count=0, platform="cursor", extra=flag)

    assert world.mailbox.ensure_calls == []


def test_manual_reuse_is_skipped_for_non_mailbox_identity(monkeypatch):
    _manual_row("oauth@example.test")
    world = _manual_world(monkeypatch, outcomes=["ok"])

    world.run(count=1, retry_count=0, extra={
        "identity_provider": "oauth_browser",
        "reuse_manual_invalid_emails": True,
    })

    assert world.mailbox.ensure_calls == []


# ══════════════════════════════════════════════════════════════════════
# 手动无效邮箱已在 ChatGPT 建号、之后失败：要变成「已建号」的注册失败行，
# 下次复用才会从 OAuth 开始，而不是再走一遍完整注册。
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("stage", ["created_oauth_failed", "created_other_failed"])
def test_created_failure_turns_manual_invalid_row_into_failed_row(stage):
    _manual_row("created@example.test")

    written = db_module.save_failed_account(
        "chatgpt", "created@example.test", "ManualPw123!",
        failure_stage=stage, failure_reason="oauth boom",
    )

    overview = _overview("created@example.test")
    assert written is True
    assert overview["lifecycle_status"] == "failed"
    assert overview["display_status"] == "failed"
    assert overview["failure_stage"] == stage
    assert overview["account_origin"] == "manual"
    failed = {row["email"]: row for row in db_module.list_reusable_failed_accounts("chatgpt")}
    assert failed["created@example.test"]["failure_stage"] == stage
    assert failed["created@example.test"]["password"] == "ManualPw123!"
    assert db_module.list_reusable_manual_invalid_accounts("chatgpt") == []


@pytest.mark.parametrize("stage", ["not_created", "unknown"])
def test_failure_without_created_account_leaves_manual_row_alone(stage):
    _manual_row("not-created@example.test")

    written = db_module.save_failed_account(
        "chatgpt", "not-created@example.test", "ManualPw123!",
        failure_stage=stage, failure_reason="boom",
    )

    overview = _overview("not-created@example.test")
    assert written is False
    assert overview["lifecycle_status"] == "registered"
    assert "failure_stage" not in overview


@pytest.mark.parametrize("kwargs", [{"valid": True}, {"account_origin": None}])
def test_created_failure_never_touches_valid_or_non_manual_rows(kwargs):
    _manual_row("keep@example.test", **kwargs)

    written = db_module.save_failed_account(
        "chatgpt", "keep@example.test", "Other123!",
        failure_stage="created_oauth_failed", failure_reason="oauth boom",
    )

    overview = _overview("keep@example.test")
    assert written is False
    assert overview["lifecycle_status"] == "registered"
    assert "failure_stage" not in overview


def test_manual_reuse_oauth_failure_is_resumed_from_oauth_next_time(monkeypatch):
    from core.registration.errors import RegistrationAttemptError

    _manual_row("oauth-fail@example.test")
    world = _manual_world(monkeypatch, outcomes=["unused", "ok"])

    def _created_then_oauth_failed(platform):
        raise RegistrationAttemptError(
            "oauth failed",
            stage="account_created",
            failure_stage="created_oauth_failed",
            email=world.attempts[-1]["email"],
            password=world.attempts[-1]["password"],
        )

    # 只有浏览器执行器续做得了 OAuth；失败账号池还要求邮箱服务能按地址收信。
    world.mailbox.can_read_address = lambda email: True
    world.on_attempt = [_created_then_oauth_failed]
    world.run(count=1, retry_count=0, max_attempts=1, executor_type="headless",
              extra={"reuse_manual_invalid_emails": True})

    overview = _overview("oauth-fail@example.test")
    assert overview["lifecycle_status"] == "failed"
    assert overview["failure_stage"] == "created_oauth_failed"

    world.run(count=1, retry_count=0, executor_type="headless",
              extra={"reuse_manual_invalid_emails": True})

    assert world.emails[-1] == "oauth-fail@example.test"
    resume = world.attempts[-1]["extra"].get("registration_resume") or {}
    assert resume.get("stage") == "account_created"
