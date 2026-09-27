"""换号重开 OAuth 的遗留 L1（验收 2026-09-27）：一个号只有一轮确认预算。

取消确认不了时，同一个号被完整确认三轮（handler 内 cleanup → escalate → 任务收尾 cleanup），
按生产参数约 7.5 分钟。预算用完之后再来问，直接得到「没确认」，不再 cancel、不再等待。
"""
from __future__ import annotations

import pytest

import core.base_sms as sms_module
import platforms.chatgpt.browser_register as br
from core.base_sms import SmsActivation, create_phone_callbacks


def _log(*_args, **_kwargs):
    return None


class _Provider:
    """cancel 恒失败，is_cancelled 恒为 False：平台一直不肯取消。"""

    def __init__(self):
        self.cancels = 0

    def get_number(self, *, service: str, country: str = ""):
        return SmsActivation(activation_id="act_1", phone_number="+15551234567")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        return "123456"

    def cancel(self, activation_id: str) -> bool:
        self.cancels += 1
        return False

    def is_cancelled(self, activation_id: str):
        return False

    def report_success(self, activation_id: str) -> bool:
        return True


def _controller(monkeypatch, provider):
    monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
    sleeps = []
    monkeypatch.setattr(sms_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    callback, cleanup = create_phone_callbacks(
        "sms_activate", {"sms_activate_api_key": "test"}, service="chatgpt", log_fn=_log,
    )
    return callback, cleanup, sleeps


def test_an_unconfirmed_number_is_not_confirmed_again(monkeypatch):
    monkeypatch.setattr(sms_module, "PHONE_RELEASE_VERIFY_ATTEMPTS", 3)
    provider = _Provider()
    callback, cleanup, sleeps = _controller(monkeypatch, provider)
    callback()

    assert callback.confirm_released() is False
    assert provider.cancels == 3
    assert len(sleeps) == 2

    assert callback.confirm_released() is False, "确认不了的号，再问一次也还是没确认"
    cleanup()
    assert provider.cancels == 3, "同一个号的确认预算已经用完，不该再取消、再等一轮"
    assert len(sleeps) == 2


def test_a_submit_failure_confirms_the_number_only_once_in_production_order(monkeypatch):
    """按生产顺序串起来：真实 handler（max_phone_attempts=1）→ escalate → 任务收尾 cleanup。"""
    monkeypatch.setattr(sms_module, "PHONE_RELEASE_VERIFY_ATTEMPTS", 3)
    provider = _Provider()
    callback, cleanup, _sleeps = _controller(monkeypatch, provider)

    def _submit_fails(_page, phone_callback, **_kwargs):
        phone_callback()
        raise RuntimeError("phone_number_in_use")

    class _Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(br, "_do_add_phone_attempt", _submit_fails)
    monkeypatch.setattr(br.time, "sleep", lambda *_args: None)

    with pytest.raises(RuntimeError, match="phone_number_in_use") as failure:
        br._handle_add_phone_challenge(
            _Page(), callback, device_id="d", user_agent="ua", log=_log, max_phone_attempts=1,
        )
    with pytest.raises(br.PhoneReleaseUnconfirmed):
        br._escalate_phone_failure(callback, failure.value, _log)
    cleanup()

    assert provider.cancels == 3, "三处各跑一轮就是 9 次：handler 内 cleanup、escalate、任务收尾 cleanup"
