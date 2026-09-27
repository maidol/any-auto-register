"""复用注册失败账号的两处返工（2026-09-27）。

B1 —— protocol 执行器续做不了 Codex OAuth。`registration_resume` 只有浏览器适配器读
（`platforms/chatgpt/plugin.py` 的 `build_browser_registration_adapter`），协议注册机拿到
一个已建号的邮箱，会跳过建号、然后因为没有 create_account 的 callback URL 失败；碰到
add_phone 也直接 raise。所以 protocol 任务不认领「已在 ChatGPT 建过号」的失败行，
留给 headless / headed 任务。

B2 —— 启用多个邮箱 provider 时（FallbackMailbox），失败行要记**实际**开出这个地址的
provider（`MailboxAccount.extra["mailbox_provider_key"]`），而不是配置里的主 provider。
"""
from __future__ import annotations

import application.tasks as tasks_module
from core import db as db_module
from core.base_mailbox import MailboxAccount, PinnedMailbox
from tests.test_failed_account_reuse import _failed, _overview, _resume_stage, _world


def _capture_log(monkeypatch, w) -> list[str]:
    lines: list[str] = []
    original = w.logger.log

    def _log(message, **kw):
        lines.append(str(message))
        return original(message, **kw)

    monkeypatch.setattr(w.logger, "log", _log)
    return lines


# ══════════════════════════════════════════════════════════════════════
# B1：protocol 执行器不认领已建号的失败行
# ══════════════════════════════════════════════════════════════════════

def test_a_protocol_task_does_not_reuse_a_created_row(monkeypatch):
    """不传 executor_type 就是 protocol（`_build_platform_instance` 的默认值）。"""
    _failed("old@reuse.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0)

    assert w.emails == ["box1@example.com"]
    assert _resume_stage(w.attempts[0]) == ""
    assert "reuse_attempts" not in _overview("old@reuse.test"), "没认领就不许记一次复用"
    assert [r["email"] for r in db_module.list_reusable_failed_accounts("chatgpt")] == ["old@reuse.test"]


def test_a_protocol_task_skips_created_other_failed_too(monkeypatch):
    _failed("half@reuse.test", "created_other_failed")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0, executor_type="protocol")

    assert w.emails == ["box1@example.com"]


def test_a_protocol_task_still_reuses_a_not_created_row(monkeypatch):
    """守卫的另一半：protocol 只是不碰已建号的行，没建号的照样复用、走完整注册。"""
    _failed("old@reuse.test", "created_oauth_failed")
    _failed("never@reuse.test", "not_created")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0)

    assert w.emails == ["never@reuse.test"]
    assert w.passwords == ["OldPw123!"]
    assert _resume_stage(w.attempts[0]) == ""


def test_a_protocol_task_says_once_why_created_rows_are_left(monkeypatch):
    _failed("old@reuse.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["ok", "ok"])
    lines = _capture_log(monkeypatch, w)

    w.run(count=2, retry_count=0)

    notes = [line for line in lines if "protocol 执行器不能续做 Codex OAuth" in line]
    assert len(notes) == 1, f"应当整个任务只说一次，实际 {len(notes)} 次：{notes}"


def test_a_headed_task_reuses_a_created_row(monkeypatch):
    _failed("old@reuse.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["ok"])
    lines = _capture_log(monkeypatch, w)

    w.run(count=1, retry_count=0, executor_type="headed")

    assert w.emails == ["old@reuse.test"]
    assert _resume_stage(w.attempts[0]) == "account_created"
    assert not [line for line in lines if "protocol 执行器不能续做 Codex OAuth" in line]


def test_the_claim_skips_created_rows_unless_asked():
    _failed("old@reuse.test", "created_oauth_failed")

    class _Readable:
        def can_read_address(self, email):
            return True

    try:
        refused = tasks_module._claim_failed_account("chatgpt", _Readable(), "", set())
        assert refused is None
        assert "reuse_attempts" not in _overview("old@reuse.test")
        taken = tasks_module._claim_failed_account("chatgpt", _Readable(), "", set(), created_ok=True)
        assert taken["email"] == "old@reuse.test"
    finally:
        tasks_module._reused_failed_accounts.clear()


# ══════════════════════════════════════════════════════════════════════
# B2：失败行记实际开出地址的 provider
# ══════════════════════════════════════════════════════════════════════

class _OneMailbox:
    def __init__(self, extra):
        self.extra = extra

    def get_email(self):
        return MailboxAccount(email="x@reuse.test", extra=dict(self.extra))


def test_the_pinned_mailbox_reports_the_provider_that_opened_the_address():
    mailbox = PinnedMailbox(_OneMailbox({"mailbox_provider_key": "backup_box"}))
    assert mailbox.pinned_provider_key == "", "还没开地址就没有 provider"
    mailbox.get_email()
    assert mailbox.pinned_provider_key == "backup_box"

    assert PinnedMailbox(_OneMailbox({})).pinned_provider_key == ""
    assert PinnedMailbox(_OneMailbox({}), pinned=MailboxAccount(email="old@reuse.test")).pinned_provider_key == ""


def _mailbox_opened_by(w, monkeypatch, provider_key: str) -> None:
    original = w.mailbox.get_email

    def _get_email():
        account = original()
        account.extra = {**(account.extra or {}), "mailbox_provider_key": provider_key}
        return account

    monkeypatch.setattr(w.mailbox, "get_email", _get_email)


def test_a_failed_row_records_the_provider_that_actually_opened_the_address(monkeypatch):
    w = _world(monkeypatch, outcomes=["boom"])  # 配置里的主 provider 是 moemail
    _mailbox_opened_by(w, monkeypatch, "backup_box")

    w.run(count=1, retry_count=0, max_attempts=1)

    assert _overview("box1@example.com")["mail_provider"] == "backup_box"


def test_without_a_provider_key_the_configured_provider_is_recorded(monkeypatch):
    w = _world(monkeypatch, outcomes=["boom"])

    w.run(count=1, retry_count=0, max_attempts=1)

    assert _overview("box1@example.com")["mail_provider"] == "moemail"


def test_the_pinned_provider_is_not_given_to_a_different_failed_email(monkeypatch):
    """失败的邮箱不是钉住的那个地址时，钉住地址的 provider 跟它没关系。"""
    w = _world(monkeypatch, outcomes=["boom"])
    _mailbox_opened_by(w, monkeypatch, "backup_box")

    w.run(count=1, retry_count=0, max_attempts=1, email="fixed@example.com")

    assert _overview("fixed@example.com")["mail_provider"] == "moemail"
