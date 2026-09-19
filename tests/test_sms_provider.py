"""SMS provider unit tests."""
from __future__ import annotations

import pytest
from core.base_sms import (
    HeroSmsCodeTimeoutError,
    HeroSmsProvider,
    SmsActivation,
    SmsActivateProvider,
    create_sms_provider,
    create_phone_callbacks,
    SMS_ACTIVATE_SERVICES,
    SMS_ACTIVATE_COUNTRIES,
    PhoneNumberAcquisitionError,
)
import core.base_sms as sms_module


class TestSmsActivateServiceMapping:
    def test_cursor_maps_to_ot(self):
        assert SMS_ACTIVATE_SERVICES["cursor"] == "ot"

    def test_chatgpt_maps_to_dr(self):
        assert SMS_ACTIVATE_SERVICES["chatgpt"] == "dr"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_SERVICES


class TestSmsActivateCountryMapping:
    def test_us_maps_to_187(self):
        assert SMS_ACTIVATE_COUNTRIES["us"] == "187"

    def test_ru_maps_to_0(self):
        assert SMS_ACTIVATE_COUNTRIES["ru"] == "0"

    def test_th_maps_to_52(self):
        assert SMS_ACTIVATE_COUNTRIES["th"] == "52"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_COUNTRIES


class TestCreateSmsProvider:
    def test_sms_activate(self):
        provider = create_sms_provider("sms_activate", {"sms_activate_api_key": "test123"})
        assert isinstance(provider, SmsActivateProvider)
        assert provider.api_key == "test123"

    def test_sms_activate_missing_key(self):
        with pytest.raises(RuntimeError, match="未配置"):
            create_sms_provider("sms_activate", {})

    def test_herosms(self):
        provider = create_sms_provider("herosms", {"herosms_api_key": "hero123"})
        assert isinstance(provider, HeroSmsProvider)
        assert provider.api_key == "hero123"
        assert provider.default_service == "dr"
        # 默认国家 2026-09-16 起是 52(泰国)：OpenAI 对美国号(187)走 WhatsApp，
        # 租来的纯 SMS 号收不到码。见 core/base_sms.py:186-190 的注释。
        assert provider.default_country == "52"

    def test_herosms_reuse_flag_parses_string_false(self):
        provider = create_sms_provider(
            "herosms",
            {
                "herosms_api_key": "hero123",
                "register_reuse_phone_to_max": "false",
            },
        )
        assert isinstance(provider, HeroSmsProvider)
        assert provider.reuse_phone_to_max is False

    def test_herosms_missing_key(self):
        with pytest.raises(RuntimeError, match="HeroSMS 未配置"):
            create_sms_provider("herosms", {})

    def test_unknown_provider(self):
        with pytest.raises(RuntimeError, match="未知"):
            create_sms_provider("unknown", {})


class TestCreatePhoneCallbacks:
    def test_returns_tuple(self):
        # This will fail on actual API call, but we can test the structure
        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="cursor",
        )
        assert callable(callback)
        assert callable(cleanup)

    def test_provider_is_created_lazily_and_cleanup_cancels_pending_activation(self, monkeypatch):
        events = []
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_1", phone_number="+15551234567")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return ""

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="us",
            log_fn=logs.append,
        )

        assert events == []
        assert callback() == "+15551234567"
        cleanup()
        assert ("get_number", "chatgpt", "us") in events
        assert ("cancel", "act_1") in events
        assert any("准备租用手机号" in item for item in logs)
        assert any("已成功租到号码" in item for item in logs)
        assert any("已释放未使用号码" in item for item in logs)

    def test_cleanup_does_not_cancel_after_success(self, monkeypatch):
        events = []
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_2", phone_number="+15557654321")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "123456"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            log_fn=logs.append,
        )

        assert callback() == "+15557654321"
        assert callback() == "123456"
        cleanup()
        assert ("report_success", "act_2") in events
        assert ("cancel", "act_2") not in events
        assert any("等待短信验证码" in item for item in logs)
        assert any("短信验证成功" in item for item in logs)

    def test_phone_callback_uses_200_seconds_only_for_herosms(self, monkeypatch):
        events = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_timeout", phone_number="+15550009999")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "123456"

            def report_success(self, activation_id: str) -> bool:
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        assert callback() == "+15550009999"
        assert callback() == "123456"
        assert events == [("get_code", "act_timeout", 200)]
        cleanup()

    def test_phone_callback_keeps_300_seconds_for_non_herosms(self, monkeypatch):
        events = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_timeout", phone_number="+15550009999")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "123456"

            def report_success(self, activation_id: str) -> bool:
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
        )

        assert callback() == "+15550009999"
        assert callback() == "123456"
        assert events == [("get_code", "act_timeout", 300)]
        cleanup()

    def test_herosms_timeout_raises_and_cancels_without_resend(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        provider = HeroSmsProvider("hero123")
        events = []
        clock = iter([0, 0, 1, 2, 3])
        monkeypatch.setattr(sms_module.time, "time", lambda: next(clock))
        monkeypatch.setattr(sms_module.time, "sleep", lambda *_args: None)
        monkeypatch.setattr(provider, "get_status_v2", lambda _id: {"status": "wait_code"})
        monkeypatch.setattr(provider, "get_status", lambda _id: {"status": "wait_code"})
        monkeypatch.setattr(provider, "get_active_activations", lambda: [])
        monkeypatch.setattr(provider, "request_resend_sms", lambda _id: events.append("hero_resend"))
        provider.set_resend_callback(lambda: events.append("openai_resend"))
        monkeypatch.setattr(provider, "cancel_activation", lambda activation_id: events.append(("cancel", activation_id)) or True)

        with pytest.raises(HeroSmsCodeTimeoutError, match="act_timeout"):
            provider.get_code("act_timeout", timeout=2)

        assert events == [("cancel", "act_timeout")]
        assert provider.last_code_result is None
        assert sms_module._HERO_SMS_CACHE is None

    def test_deferred_success_provider_reports_on_cleanup_for_legacy_callers(self, monkeypatch):
        events = []

        class FakeProvider:
            auto_report_success_on_code = False

            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_deferred", phone_number="+15550001111")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "111222"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="cursor",
        )

        assert callback() == "+15550001111"
        assert callback() == "111222"
        cleanup()
        assert ("report_success", "act_deferred") in events
        assert ("cancel", "act_deferred") not in events

    def test_first_number_fetch_failure_retries_before_returning_number(self, monkeypatch):
        events = []

        class FakeProvider:
            def __init__(self):
                self.calls = 0

            def get_number(self, *, service: str, country: str = ""):
                self.calls += 1
                events.append(("get_number", self.calls, service, country))
                if self.calls == 1:
                    raise RuntimeError("temporary failure")
                return SmsActivation(activation_id="act_retry", phone_number="+66123456789")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "654321"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="th",
        )

        assert callback() == "+66123456789"
        assert [event[0:2] for event in events if event[0] == "get_number"] == [
            ("get_number", 1),
            ("get_number", 2),
        ]
        assert callback() == "654321"
        cleanup()
        assert ("report_success", "act_retry") in events

    def test_number_fetch_retries_three_times_after_initial_failure(self, monkeypatch):
        class FakeProvider:
            def __init__(self):
                self.calls = 0

            def get_number(self, *, service: str, country: str = ""):
                self.calls += 1
                if self.calls <= 3:
                    raise RuntimeError("NO_NUMBERS")
                return SmsActivation(activation_id="act_retry_limit", phone_number="+66111111111")

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="th",
        )

        assert callback() == "+66111111111"
        assert provider.calls == 4
        cleanup()

    def test_number_fetch_fails_fast_for_invalid_api_key(self, monkeypatch):
        class FakeProvider:
            def __init__(self):
                self.calls = 0

            def get_number(self, *, service: str, country: str = ""):
                self.calls += 1
                raise RuntimeError("BAD_KEY")

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="th",
        )

        with pytest.raises(RuntimeError, match="BAD_KEY"):
            callback()
        assert provider.calls == 1
        cleanup()

    def test_sms_activate_no_numbers_retries_from_real_provider_error(self, monkeypatch):
        responses = ["NO_NUMBERS", "NO_NUMBERS", "NO_NUMBERS", "ACCESS_NUMBER:act_real:+66123456789"]
        calls = []
        sleeps = []
        monkeypatch.setattr("core.base_sms.time.sleep", lambda seconds: sleeps.append(seconds))

        def fake_request(self, action: str, **params):
            calls.append(action)
            if action == "getNumber":
                return responses.pop(0)
            return "ACCESS"

        monkeypatch.setattr(SmsActivateProvider, "_request", fake_request)
        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="52",
        )

        assert callback() == "+66123456789"
        assert calls.count("getNumber") == 4
        assert sleeps == [2, 2, 2]
        cleanup()

    def test_herosms_auto_country_falls_back_after_non_retryable_error(self, monkeypatch):
        class FakeProvider(HeroSmsProvider):
            def __init__(self):
                self.calls = []

            def get_best_country(self, *, service: str, min_stock: int = 20, max_price: float = 0):
                return "99"

            def get_number(self, *, service: str, country: str = ""):
                self.calls.append(country)
                if country == "99":
                    raise RuntimeError("invalid country")
                return SmsActivation(activation_id="act_auto_fallback", phone_number="+66999999999")

            def cancel(self, activation_id: str) -> bool:
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {
                "herosms_api_key": "test",
                "herosms_auto_country": True,
            },
            service="chatgpt",
            country="52",
        )

        assert callback() == "+66999999999"
        assert provider.calls == ["99", "52"]
        cleanup()

    def test_herosms_auto_country_and_fallback_share_retry_budget(self, monkeypatch):
        class FakeProvider(HeroSmsProvider):
            def __init__(self):
                self.calls = []

            def get_best_country(self, *, service: str, min_stock: int = 20, max_price: float = 0):
                return "99"

            def get_number(self, *, service: str, country: str = ""):
                self.calls.append(country)
                if len(self.calls) < 4:
                    raise RuntimeError("NO_NUMBERS")
                return SmsActivation(activation_id="act_auto_retry", phone_number="+66999999999")

            def cancel(self, activation_id: str) -> bool:
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {
                "herosms_api_key": "test",
                "herosms_auto_country": True,
            },
            service="chatgpt",
            country="52",
        )

        assert callback() == "+66999999999"
        assert provider.calls == ["99", "52", "52", "52"]
        cleanup()

    def test_country_rotation_selects_another_provider_country(self, monkeypatch):
        events = []

        class FakeProvider:
            def get_country_candidates(self, *, service: str, exclude=()):
                events.append(("candidates", service, tuple(exclude)))
                return [country for country in ["52", "66", "7"] if country not in set(exclude)]

            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(
                    activation_id=f"act_{country}",
                    phone_number=f"+{country}123456789",
                    country=country,
                )

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
            country="187",
        )

        assert callback() == "+187123456789"
        cleanup()
        assert callback.rotate_country() is True
        callback.rearm()
        assert callback() == "+52123456789"
        cleanup()
        assert callback.rotate_country() is True
        callback.rearm()
        assert callback() == "+66123456789"
        assert [event for event in events if event[0] == "get_number"] == [
            ("get_number", "chatgpt", "187"),
            ("get_number", "chatgpt", "52"),
            ("get_number", "chatgpt", "66"),
        ]
        cleanup()

    def test_country_rotation_query_failure_is_distinct_from_empty_inventory(self, monkeypatch):
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_query", phone_number="+52123456789")

            def get_country_candidates(self, *, service: str, exclude=()):
                raise ConnectionError("country inventory unavailable")

            def cancel(self, activation_id: str) -> bool:
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
            country="52",
            log_fn=logs.append,
        )

        assert callback() == "+52123456789"
        assert callback.rotate_country() is None
        assert callback._failed_countries == set()
        assert any("国家轮换查询失败" in message for message in logs)
        cleanup()

    def test_country_rotation_uses_configured_fallback_when_inventory_query_fails(self, monkeypatch):
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_fallback", phone_number="+66123456789")

            def get_country_candidates(self, *, service: str, exclude=()):
                raise ConnectionError("inventory unavailable")

            def cancel(self, activation_id: str) -> bool:
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test", "sms_country": "66"},
            service="chatgpt",
            country="52",
            log_fn=logs.append,
        )

        assert callback() == "+66123456789"
        assert callback.rotate_country() is True
        assert callback.country == "66"
        assert any("备用国家" in message for message in logs)
        cleanup()

    def test_country_rotation_empty_inventory_is_not_query_failure(self, monkeypatch):
        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_empty", phone_number="+52123456789")

            def get_country_candidates(self, *, service: str, exclude=()):
                return []

            def cancel(self, activation_id: str) -> bool:
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
            country="52",
        )

        assert callback() == "+52123456789"
        assert callback.rotate_country() is False
        assert callback._failed_countries == {"52"}
        cleanup()

    def test_herosms_get_top_countries_distinguishes_query_failure(self, monkeypatch):
        provider = HeroSmsProvider("hero123")

        def fail_request(params, **kwargs):
            raise ConnectionError("inventory down")

        monkeypatch.setattr(provider, "_request", fail_request)
        with pytest.raises(RuntimeError, match="国家库存查询失败"):
            provider.get_top_countries(service="chatgpt", strict=True)

    def test_herosms_get_top_countries_accepts_valid_empty_inventory(self, monkeypatch):
        provider = HeroSmsProvider("hero123")

        class Response:
            def json(self):
                return {}

        monkeypatch.setattr(provider, "_request", lambda params, **kwargs: Response())
        assert provider.get_top_countries(service="chatgpt") == []

    def test_herosms_country_candidates_filter_inventory_rows(self, monkeypatch):
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(
            provider,
            "get_top_countries",
            lambda service=None, strict=False: [
                {"country": "52", "count": 10, "price": 0.1},
                {"country": "66", "count": 0, "price": 0.1},
                {"country": "7", "count": 5, "price": 0.2},
                {"country": "52", "count": 20, "price": 0.3},
                {"country": "", "count": 50, "price": 0.1},
            ],
        )
        assert provider.get_country_candidates(service="chatgpt", exclude={"52"}) == ["7"]

    def test_herosms_country_candidates_propagates_inventory_query_failure(self, monkeypatch):
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(
            provider,
            "get_top_countries",
            lambda service=None, strict=False: (_ for _ in ()).throw(ConnectionError("inventory down")),
        )

        with pytest.raises(ConnectionError, match="inventory down"):
            provider.get_country_candidates(service="chatgpt")

    def test_country_rotation_state_resets_after_success(self, monkeypatch):
        class FakeProvider:
            auto_report_success_on_code = True

            def get_country_candidates(self, *, service: str, exclude=()):
                return ["66"]

            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_success", phone_number="+66999999999")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                return "123456"

            def report_success(self, activation_id: str) -> bool:
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
            country="52",
        )

        assert callback.rotate_country() is True
        assert callback() == "+66999999999"
        assert callback() == "123456"
        assert callback._failed_countries == set()
        assert callback._country_rotation_active is False
        cleanup()

    def test_base_provider_candidates_default_is_undecided_not_empty(self):
        class MinimalProvider(sms_module.BaseSmsProvider):
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="a", phone_number="+1")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                return "1"

            def cancel(self, activation_id: str) -> bool:
                return True

        assert MinimalProvider().get_country_candidates(service="chatgpt") is None, (
            "没有库存查询能力的 provider 返回 [] 会被读成『查过了，没有别的国家』"
        )

    def test_rotation_is_undecided_when_provider_has_no_inventory_lookup(self, monkeypatch):
        class NoLookupProvider(sms_module.BaseSmsProvider):
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="a", phone_number="+52123456789")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                return "1"

            def cancel(self, activation_id: str) -> bool:
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda k, c: NoLookupProvider())
        callback, cleanup = create_phone_callbacks(
            "sms_activate", {"sms_activate_api_key": "t"},
            service="chatgpt", country="52",
        )
        assert callback.rotate_country() is None, (
            "sms_activate / smsbower 没有库存查询，不能把『我不知道』说成『没有别的国家』"
        )
        cleanup()

    def test_herosms_top_countries_strict_rejects_http200_error_payload(self, monkeypatch):
        provider = HeroSmsProvider("hero123")

        class Response:
            def json(self):
                return {"status": 0, "message": "No access", "data": []}

        monkeypatch.setattr(provider, "_request", lambda params, **kwargs: Response())
        with pytest.raises(RuntimeError):
            provider.get_top_countries(service="chatgpt", strict=True)

    def test_herosms_top_countries_strict_accepts_genuinely_sold_out(self, monkeypatch):
        provider = HeroSmsProvider("hero123")

        class Response:
            def json(self):
                return {"52": {"chatgpt": {"cost": 0.5, "count": 0}}}

        monkeypatch.setattr(provider, "_request", lambda params, **kwargs: Response())
        assert provider.get_top_countries(service="chatgpt", strict=True) == [], (
            "报文里出现了这个服务、只是库存为 0，这是『真的没货』，不能抛"
        )

    def test_rotation_end_to_end_when_herosms_request_fails(self, monkeypatch):
        provider = HeroSmsProvider("hero123")

        def fail_request(params, **kwargs):
            raise ConnectionError("inventory down")

        monkeypatch.setattr(provider, "_request", fail_request)
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda k, c: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms", {"herosms_api_key": "t"}, service="chatgpt", country="52",
        )
        assert callback.rotate_country() is None, (
            "这条不桩 get_top_countries、也不桩 get_country_candidates："
            "它钉的是两者之间的接线（strict=True）"
        )
        assert callback._failed_countries == set()
        cleanup()

    def test_rotation_end_to_end_when_herosms_returns_error_payload(self, monkeypatch):
        provider = HeroSmsProvider("hero123")

        class Response:
            def json(self):
                return {"status": 0, "message": "No access", "data": []}

        monkeypatch.setattr(provider, "_request", lambda params, **kwargs: Response())
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda k, c: provider)
        callback, cleanup = create_phone_callbacks(
            "herosms", {"herosms_api_key": "t"}, service="chatgpt", country="52",
        )
        assert callback.rotate_country() is None, (
            "HTTP 200 + JSON 错误体也是『查不到』，不是『没有别的国家』"
        )
        cleanup()

    def test_rotation_logs_a_reason_before_returning_false(self, monkeypatch):
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="a", phone_number="+52123456789")

            def get_country_candidates(self, *, service: str, exclude=()):
                return []

            def cancel(self, activation_id: str) -> bool:
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda k, c: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "herosms", {"herosms_api_key": "t"},
            service="chatgpt", country="52", log_fn=logs.append,
        )
        assert callback.rotate_country() is False
        assert any("国家轮换" in m for m in logs), (
            f"返回 False 会让整轮注册终止，终止前必须留下一条日志，实际 logs={logs}"
        )
        cleanup()

    def test_herosms_number_fetch_failure_releases_verify_lock(self, monkeypatch):
        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                raise RuntimeError("temporary failure")

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        with pytest.raises(RuntimeError, match="temporary failure"):
            callback()

        assert callback._verify_lock_acquired is False
        cleanup()

    def test_mark_send_succeeded_delegates_to_provider(self, monkeypatch):
        events = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_sent", phone_number="+15551234567")

            def mark_send_succeeded(self, activation_id: str) -> None:
                events.append(("mark_send_succeeded", activation_id))

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        assert callback() == "+15551234567"
        callback.mark_send_succeeded()
        cleanup()
        assert ("mark_send_succeeded", "act_sent") in events


class TestSmsActivateProviderCountryResolution:
    def test_get_number_accepts_numeric_country_id(self, monkeypatch):
        captured = {}

        def fake_request(self, action: str, **params):
            captured["action"] = action
            captured["params"] = params
            return "NO_NUMBERS"

        monkeypatch.setattr(SmsActivateProvider, "_request", fake_request)
        provider = SmsActivateProvider("test123", default_country="ru")

        with pytest.raises(RuntimeError, match="NO_NUMBERS|无可用号码"):
            provider.get_number(service="chatgpt", country="52")

        assert captured["action"] == "getNumber"
        assert captured["params"]["country"] == "52"

    def test_no_numbers_error_is_explicitly_retryable(self, monkeypatch):
        monkeypatch.setattr(SmsActivateProvider, "_request", lambda self, action, **params: "NO_NUMBERS")
        provider = SmsActivateProvider("test123", default_country="ru")

        with pytest.raises(PhoneNumberAcquisitionError) as exc_info:
            provider.get_number(service="chatgpt", country="52")

        assert exc_info.value.retryable is True
        assert "NO_NUMBERS" in str(exc_info.value)


class TestHeroSmsProvider:
    def test_get_number_uses_v2_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        calls = []

        class FakeResp:
            text = '{"activationId":"act_1","phoneNumber":"5551234","countryPhoneCode":"1","activationCost":"0.6"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"activationId": "act_1", "phoneNumber": "5551234", "countryPhoneCode": "1", "activationCost": "0.6"}

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params)
            return FakeResp()

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(service="chatgpt", country="187")

        assert activation.activation_id == "act_1"
        assert activation.phone_number == "+15551234"
        assert [call["action"] for call in calls] == ["getPrices", "getNumberV2"]

    def test_herosms_v1_no_numbers_survives_v2_deterministic_error(self, monkeypatch, tmp_path):
        """V2's deterministic error must not suppress V1's retryable NO_NUMBERS."""
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)

        class FakeResp:
            def __init__(self, text):
                self.text = text
                self.status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("not json")

        def fake_get(url, params, timeout=30, proxies=None):
            action = params["action"]
            if action == "getPrices":
                return FakeResp("{}")
            if action == "getNumberV2":
                return FakeResp('{"error":"UNPROCESSABLE_ENTITY"}')
            return FakeResp("NO_NUMBERS")

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")

        with pytest.raises(PhoneNumberAcquisitionError) as exc_info:
            provider.get_number(service="chatgpt", country="52")

        assert "UNPROCESSABLE_ENTITY" in str(exc_info.value)
        assert "NO_NUMBERS" in str(exc_info.value)
        assert exc_info.value.retryable is True

    def test_get_number_falls_back_to_v1_text(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        calls = []

        class FakeResp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("not json")

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params["action"])
            if params["action"] == "getNumberV2":
                return FakeResp("BAD")
            return FakeResp("ACCESS_NUMBER:act_2:15557654321")

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(service="chatgpt", country="187")

        assert activation.activation_id == "act_2"
        assert activation.phone_number == "+15557654321"
        assert calls == ["getPrices", "getNumberV2", "getNumber"]

    def test_get_code_skips_attempted_sms_event(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_3",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        provider = HeroSmsProvider("hero123")
        first = {"status": "ok", "code": "111111", "sms_key": "sms_1", "allow_same_code": True}
        second = {"status": "ok", "code": "222222", "sms_key": "sms_2", "allow_same_code": True}
        results = [first, second]

        monkeypatch.setattr(provider, "get_status_v2", lambda activation_id: results.pop(0))
        monkeypatch.setattr(provider, "get_status", lambda activation_id: {"status": "wait_code"})
        monkeypatch.setattr(provider, "get_active_activations", lambda: [])
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: True)

        assert provider.get_code("act_3", timeout=1) == "111111"
        provider.mark_code_failed("act_3", "invalid otp")
        assert provider.get_code("act_3", timeout=1) == "222222"

    def test_mark_send_succeeded_sets_sms_sent_status(self, monkeypatch):
        calls = []
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(provider, "set_status", lambda activation_id, status: calls.append((activation_id, status)) or "ACCESS_READY")

        provider.mark_send_succeeded("act_4")

        assert calls == [("act_4", 1)]

    def test_mark_code_failed_triggers_openai_and_herosms_resend(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_5",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123")
        provider.last_code_result = {"code": "333333", "sms_key": "sms_3"}
        provider.set_resend_callback(lambda: events.append(("openai_resend",)))
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: events.append(("hero_resend", activation_id)) or True)

        provider.mark_code_failed("act_5", "invalid otp")

        assert ("openai_resend",) in events
        assert ("hero_resend", "act_5") in events
        assert "333333" in sms_module._HERO_SMS_CACHE["used_codes"]
        assert "sms_3" in sms_module._HERO_SMS_CACHE["attempted_sms_keys"]

    def test_report_success_finishes_activation_when_reuse_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_6",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123", reuse_phone_to_max=False)
        provider.last_code_result = {"code": "444444", "sms_key": "sms_4"}
        monkeypatch.setattr(provider, "finish_activation", lambda activation_id: events.append(("finish", activation_id)) or True)

        assert provider.report_success("act_6") is True

        assert events == [("finish", "act_6")]
        assert sms_module._HERO_SMS_CACHE is None


class TestSmsActivation:
    def test_dataclass(self):
        a = SmsActivation(activation_id="123", phone_number="+79001234567")
        assert a.activation_id == "123"
        assert a.phone_number == "+79001234567"
        assert a.country == ""

    def test_with_country(self):
        a = SmsActivation(activation_id="1", phone_number="+1555", country="us")
        assert a.country == "us"
