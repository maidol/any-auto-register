"""ba10042 评审发现的注册链路回归。

每条测试对应一个在未修复代码上可复现的缺陷，见
reviews/any-auto-register/2026-09-15-ba10042-verdict.md
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeMailbox:
    """收件箱随邮件到达而增长；wait_for_code 返回第一封不在 before_ids 里的邮件。"""

    def __init__(self):
        self.mails = []

    def get_current_ids(self, account):
        return {mid for mid, _ in self.mails}

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                      code_pattern=None):
        seen = set(before_ids or [])
        for mid, code in self.mails:
            if mid in seen:
                continue
            return code
        return ""


def _ctx_with(mailbox, before_ids):
    from core.registration.models import RegistrationContext

    return RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=mailbox),
        identity=SimpleNamespace(mailbox_account=object(), before_ids=before_ids),
        config=SimpleNamespace(executor_type="headless", extra={}, proxy=None),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )


def test_otp_callback_second_call_must_not_return_the_first_otp():
    from core.registration.helpers import build_otp_callback

    mailbox = _FakeMailbox()
    ctx = _ctx_with(mailbox, before_ids=mailbox.get_current_ids(None))
    cb = build_otp_callback(ctx, timeout=5)

    mailbox.mails.append(("m1", "111111"))
    assert cb() == "111111"

    mailbox.mails.append(("m2", "222222"))
    assert cb() == "222222", "second OTP round replayed the first mail's code"


def test_protocol_mailbox_service_does_not_replay_the_previous_otp():
    from platforms.chatgpt.protocol_mailbox import _MailboxEmailService

    mailbox = _FakeMailbox()
    acct = SimpleNamespace(email="u@e.com", account_id="a1")
    svc = _MailboxEmailService(mailbox=mailbox, mailbox_account=acct, provider="x")
    svc.create_email()

    mailbox.mails.append(("m1", "111111"))
    assert svc.get_verification_code(timeout=5, otp_sent_at=1000.0) == "111111"

    mailbox.mails.append(("m2", "222222"))
    assert svc.get_verification_code(timeout=5, otp_sent_at=2000.0) == "222222", \
        "protocol flow replayed the first mail's code"


def test_phone_callback_controller_is_reusable_after_success():
    from core.base_sms import PhoneCallbackController, SmsActivation

    ctrl = PhoneCallbackController("herosms", {}, service="dr", country="52")
    ctrl.provider = SimpleNamespace(
        get_number=lambda service, country="": SmsActivation(
            activation_id="a2", phone_number="+66900000002", metadata={}),
        get_code=lambda aid, timeout=180: "654321",
        auto_report_success_on_code=True,
        report_success=lambda aid: True,
    )
    ctrl.phase = "done"
    ctrl.completed = True

    assert ctrl() != "", "second browser got an empty string, not a number and not an error"


def test_chatgpt_payment_module_imports():
    import importlib

    importlib.import_module("platforms.chatgpt.payment")


def test_chatgpt_check_valid_reports_plan_from_subscription_details(monkeypatch):
    from core.base_platform import RegisterConfig
    from platforms.chatgpt.plugin import ChatGPTPlatform

    payment = pytest.importorskip("platforms.chatgpt.payment")
    import core.proxy_pool as pp

    monkeypatch.setattr(pp.proxy_pool, "get_next", lambda region="": None)
    monkeypatch.setattr(
        payment, "fetch_subscription_status_details",
        lambda account, proxy=None: {"status": "plus", "source": "wham"},
    )

    platform = object.__new__(ChatGPTPlatform)
    platform.config = RegisterConfig()
    account = SimpleNamespace(
        email="u@e.com", token="at_live", region="",
        extra={"access_token": "at_live", "id_token": "", "cookies": ""},
    )

    assert platform.check_valid(account) is True
    assert platform.get_last_check_overview().get("plan") == "plus"
