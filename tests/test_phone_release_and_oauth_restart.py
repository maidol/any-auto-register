"""需求 20260926 第 1 点：换号重试要重开 Codex OAuth，号码取消后要确认。

号码等验证码超时（或提交失败）之后，页面停在验证码页，同一个页面上换号走不下去。
所以换号 = 确认旧号已取消 → 关浏览器 → 重新开始一次 OAuth → 租新号。
号码、activation 都不跨 OAuth；跨 OAuth 的只有「这次 run 还能用几个号」这个计数，
phone_retry_count=N 表示整个 run 最多用 N+1 个号码。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import core.base_sms as sms_module
import platforms.chatgpt.browser_register as br
from core.base_sms import (
    HeroSmsCodeTimeoutError,
    HeroSmsProvider,
    SmsActivateProvider,
    SmsActivation,
    create_phone_callbacks,
)
from core.registration.errors import RegistrationAttemptError


def _log(*_args, **_kwargs):
    return None


# ══════════════════════════════════════════════════════════════════════
# core/base_sms.py：取消之后要确认
# ══════════════════════════════════════════════════════════════════════

class _CancelProvider:
    """记录 cancel / is_cancelled 调用；statuses 依次作为 is_cancelled 的返回值。"""

    def __init__(self, statuses=(True,), *, code_timeout=False):
        self.events = []
        self._statuses = list(statuses)
        self._code_timeout = code_timeout

    def get_number(self, *, service: str, country: str = ""):
        self.events.append(("get_number",))
        return SmsActivation(activation_id="act_1", phone_number="+15551234567")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        self.events.append(("get_code", activation_id))
        if self._code_timeout:
            raise HeroSmsCodeTimeoutError(activation_id)
        return "123456"

    def cancel(self, activation_id: str) -> bool:
        self.events.append(("cancel", activation_id))
        return False

    def is_cancelled(self, activation_id: str):
        self.events.append(("is_cancelled", activation_id))
        return self._statuses.pop(0) if self._statuses else False

    def report_success(self, activation_id: str) -> bool:
        self.events.append(("report_success", activation_id))
        return True


def _controller(monkeypatch, provider):
    monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
    sleeps = []
    monkeypatch.setattr(sms_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    logs = []
    callback, cleanup = create_phone_callbacks(
        "sms_activate", {"sms_activate_api_key": "test"}, service="chatgpt", log_fn=logs.append,
    )
    return callback, cleanup, sleeps, logs


def test_confirm_released_retries_the_cancel_until_the_status_says_cancelled(monkeypatch):
    """购买后 2 分钟内平台拒绝取消，所以要隔一段时间重试，直到状态查询说已取消。"""
    provider = _CancelProvider(statuses=(False, False, True))
    callback, _cleanup, sleeps, logs = _controller(monkeypatch, provider)
    assert callback() == "+15551234567"

    assert callback.confirm_released() is True

    assert [e for e in provider.events if e[0] == "cancel"] == [("cancel", "act_1")] * 3
    assert sleeps == [sms_module.PHONE_RELEASE_VERIFY_INTERVAL_SECONDS] * 2
    assert any("确认取消" in item for item in logs)
    assert callback.confirm_released() is True
    assert [e for e in provider.events if e[0] == "cancel"] == [("cancel", "act_1")] * 3, (
        "已经确认取消的号码不该再取消一次"
    )


def test_confirm_released_is_false_when_the_provider_cannot_report_the_status(monkeypatch):
    provider = _CancelProvider()
    provider.is_cancelled = lambda activation_id: None
    callback, _cleanup, sleeps, _logs = _controller(monkeypatch, provider)
    callback()

    assert callback.confirm_released() is False
    assert [e for e in provider.events if e[0] == "cancel"] == [("cancel", "act_1")]
    assert sleeps == []


def test_confirm_released_gives_up_after_the_retry_budget(monkeypatch):
    monkeypatch.setattr(sms_module, "PHONE_RELEASE_VERIFY_ATTEMPTS", 3)
    provider = _CancelProvider(statuses=())
    callback, _cleanup, sleeps, logs = _controller(monkeypatch, provider)
    callback()

    assert callback.confirm_released() is False
    assert [e for e in provider.events if e[0] == "cancel"] == [("cancel", "act_1")] * 3
    assert len(sleeps) == 2, "最后一次失败之后不该再睡"
    assert any("号码取消未确认" in item for item in logs)


def test_confirm_released_does_not_cancel_a_number_that_was_used_successfully(monkeypatch):
    provider = _CancelProvider()
    callback, _cleanup, _sleeps, _logs = _controller(monkeypatch, provider)
    callback()
    assert callback() == "123456"

    assert callback.confirm_released() is True
    assert not [e for e in provider.events if e[0] in ("cancel", "is_cancelled")]


def test_cleanup_confirms_the_cancel_of_a_timed_out_number(monkeypatch):
    """超时会把 activation 清掉；收尾时仍然要确认最后那个号已取消。"""
    provider = _CancelProvider(statuses=(True,), code_timeout=True)
    callback, cleanup, _sleeps, _logs = _controller(monkeypatch, provider)
    callback()
    with pytest.raises(HeroSmsCodeTimeoutError):
        callback()
    assert callback.activation is None

    cleanup()

    assert ("cancel", "act_1") in provider.events
    assert ("is_cancelled", "act_1") in provider.events


def test_herosms_is_cancelled_reads_status_cancel(monkeypatch):
    provider = HeroSmsProvider(api_key="k")
    answers = iter(["STATUS_CANCEL", "STATUS_WAIT_CODE"])
    monkeypatch.setattr(provider, "_request", lambda params: SimpleNamespace(text=next(answers)))

    assert provider.is_cancelled("act_1") is True
    assert provider.is_cancelled("act_1") is False


def test_sms_activate_is_cancelled_reads_status_cancel(monkeypatch):
    provider = SmsActivateProvider(api_key="k")
    answers = iter(["STATUS_CANCEL", "STATUS_WAIT_CODE"])
    monkeypatch.setattr(provider, "_request", lambda action, **params: next(answers))

    assert provider.is_cancelled("act_1") is True
    assert provider.is_cancelled("act_1") is False


# ══════════════════════════════════════════════════════════════════════
# browser_register.py：单号失败之后怎么收口
# ══════════════════════════════════════════════════════════════════════

class _EscalateCallback:
    def __init__(self, *, confirmed=True, rotation=True):
        self.events = []
        self._confirmed = confirmed
        self._rotation = rotation

    def confirm_released(self):
        self.events.append("confirm")
        return self._confirmed

    def rotate_country(self):
        self.events.append("rotate")
        return self._rotation

    def rearm(self):
        self.events.append("rearm")


WHATSAPP = "手机号提交失败: We couldn't send a text message to this phone number, so we switched to WhatsApp."


def test_escalate_confirms_the_cancel_then_asks_for_a_new_oauth():
    callback = _EscalateCallback()
    with pytest.raises(br.PhoneRestartRequired):
        br._escalate_phone_failure(callback, HeroSmsCodeTimeoutError("act_1"), _log)
    assert callback.events == ["confirm", "rearm"]


def test_escalate_refuses_to_go_on_when_the_cancel_is_unconfirmed():
    callback = _EscalateCallback(confirmed=False)
    with pytest.raises(br.PhoneReleaseUnconfirmed):
        br._escalate_phone_failure(callback, RuntimeError("未获取到短信验证码"), _log)
    assert callback.events == ["confirm"], "取消没确认就不许复位、不许换国家"


def test_escalate_rotates_the_country_for_a_whatsapp_failure():
    callback = _EscalateCallback()
    with pytest.raises(br.PhoneRestartRequired):
        br._escalate_phone_failure(callback, RuntimeError(WHATSAPP), _log)
    assert callback.events == ["confirm", "rotate", "rearm"]


def test_escalate_stops_when_there_is_no_other_country():
    callback = _EscalateCallback(rotation=False)
    with pytest.raises(RuntimeError, match="没有可用的其他国家号码") as exc_info:
        br._escalate_phone_failure(callback, RuntimeError(WHATSAPP), _log)
    assert not isinstance(exc_info.value, br.PhoneRestartRequired)
    assert callback.events == ["confirm", "rotate"]


def test_escalate_leaves_other_errors_alone():
    callback = _EscalateCallback()
    assert br._escalate_phone_failure(callback, RuntimeError("OAuth 页面错误: boom"), _log) is None
    assert callback.events == []


class _AddPhonePage:
    url = "https://auth.openai.com/add-phone"

    def goto(self, *_args, **_kwargs):
        return None

    def evaluate(self, *_args, **_kwargs):
        return "UA"


def test_oauth_uses_one_number_and_hands_the_failure_up(monkeypatch):
    """OAuth 里的 add_phone 只用一个号；失败不能被吞成 None，要让上层重开 OAuth。"""
    seen = {}

    def _fake_handler(page, phone_callback, **kwargs):
        seen.update(kwargs)
        raise HeroSmsCodeTimeoutError("act_1")

    monkeypatch.setattr(br, "_derive_oauth_state_from_page", lambda page: {"page_type": "add_phone"})
    monkeypatch.setattr(br, "_get_page_oauth_url", lambda page: "")
    monkeypatch.setattr(br, "_handle_add_phone_challenge", _fake_handler)
    callback = _EscalateCallback()

    with pytest.raises(br.PhoneRestartRequired):
        br._do_codex_oauth(
            _AddPhonePage(), {}, "u@example.com", "Pw123456!x",
            None, callback, None, _log, 3,
        )
    assert seen.get("max_phone_attempts") == 1
    assert callback.events == ["confirm", "rearm"]


class _CountingBrowser:
    def __init__(self, record):
        self._record = record

    def __enter__(self):
        self._record.append("open")
        return SimpleNamespace(new_page=lambda: object())

    def __exit__(self, *_exc):
        self._record.append("close")
        return False


def _worker(monkeypatch, max_phone_attempts, oauth_outcomes):
    record = []
    outcomes = list(oauth_outcomes)
    monkeypatch.setattr(br, "Camoufox", lambda **_kw: _CountingBrowser(record))

    def _fake_oauth(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(br, "_do_codex_oauth", _fake_oauth)
    worker = br.ChatGPTBrowserRegister(headless=True, log_fn=_log, max_phone_attempts=max_phone_attempts)
    return worker, record


TOKENS = {"account_id": "acc", "access_token": "at", "refresh_token": "rt", "id_token": "it"}


def test_each_number_gets_a_fresh_browser_and_a_fresh_oauth(monkeypatch):
    worker, record = _worker(monkeypatch, 3, [
        br.PhoneRestartRequired("t1"), br.PhoneRestartRequired("t2"), TOKENS,
    ])
    assert worker._retry_oauth_fresh_browser("u@example.com", "Pw123456!x") == TOKENS
    assert record == ["open", "close"] * 3


def test_oauth_restarts_stop_when_the_numbers_run_out(monkeypatch):
    worker, record = _worker(monkeypatch, 2, [
        br.PhoneRestartRequired("t1"), br.PhoneRestartRequired("t2"), TOKENS,
    ])
    assert worker._retry_oauth_fresh_browser("u@example.com", "Pw123456!x") is None
    assert record == ["open", "close"] * 2, "phone_retry_count=1 → 最多 2 个号码 = 最多 2 次 OAuth"


def test_no_restart_after_an_unconfirmed_cancel(monkeypatch):
    worker, record = _worker(monkeypatch, 3, [br.PhoneReleaseUnconfirmed("x"), TOKENS])
    assert worker._retry_oauth_fresh_browser("u@example.com", "Pw123456!x") is None
    assert record == ["open", "close"]


def _signup_worker(monkeypatch, max_phone_attempts):
    def _flow(page, email, password, otp_callback, phone_callback, log, max_phone_attempts, progress=None):
        progress["account_exists"] = True
        raise HeroSmsCodeTimeoutError("act_signup")

    oauth_calls = []
    monkeypatch.setattr(br, "_browser_registration_flow", _flow)
    monkeypatch.setattr(br, "_get_cookies", lambda page: {})
    monkeypatch.setattr(br, "Camoufox", lambda **_kw: _CountingBrowser([]))
    monkeypatch.setattr(
        br.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser",
        lambda self, email, password: oauth_calls.append(self._phone_numbers_left) or TOKENS,
    )
    callback = _EscalateCallback()
    worker = br.ChatGPTBrowserRegister(
        headless=True, log_fn=_log, phone_callback=callback, max_phone_attempts=max_phone_attempts,
    )
    return worker, callback, oauth_calls


def test_a_signup_phone_failure_hands_over_to_oauth_with_one_number_used(monkeypatch):
    worker, callback, oauth_calls = _signup_worker(monkeypatch, 3)
    result = worker.run(email="u@example.com", password="Pw123456!x")
    assert result["refresh_token"] == "rt"
    assert callback.events == ["confirm", "rearm"]
    assert oauth_calls == [2]


def test_a_signup_phone_failure_with_no_numbers_left_reports_a_created_account(monkeypatch):
    worker, _callback, oauth_calls = _signup_worker(monkeypatch, 1)
    with pytest.raises(RegistrationAttemptError, match="换号次数已用完") as err:
        worker.run(email="u@example.com", password="Pw123456!x")
    assert err.value.stage == "account_created"
    assert oauth_calls == []


def test_signup_state_machine_uses_one_number_per_add_phone():
    src = inspect.getsource(br._browser_registration_flow)
    assert src.count("max_phone_attempts=1,") == 2
    assert "max_phone_attempts=max_phone_attempts" not in src
