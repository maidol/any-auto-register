from __future__ import annotations

from application.tasks import _hero_task_reuse_policy
from application.tasks import _resolve_sms_provider_for_task
from infrastructure.provider_settings_repository import ProviderSettingsRepository


def test_resolve_sms_provider_for_task_uses_inline_herosms_default():
    provider_key, settings = _resolve_sms_provider_for_task({
        "sms_provider": "herosms_api",
        "herosms_api_key": "hero123",
        "sms_service": "dr",
        "sms_country": "187",
    })

    assert provider_key == "herosms_api"
    assert settings["herosms_api_key"] == "hero123"
    assert settings["sms_service"] == "dr"


def test_resolve_sms_provider_for_task_allows_inline_override():
    provider_key, settings = _resolve_sms_provider_for_task({
        "sms_provider": "herosms",
        "herosms_api_key": "inline",
        "sms_country": "52",
    })

    assert provider_key == "herosms"
    assert settings["herosms_api_key"] == "inline"
    assert settings["sms_country"] == "52"


def test_openai_dr_task_policy_disables_reuse_even_when_enabled():
    settings = {
        "herosms_api_key": "inline",
        "sms_service": "dr",
        "register_reuse_phone_to_max": "true",
        "register_phone_extra_max": "3",
    }
    provider_key = "herosms_api"
    herosms_dr = provider_key in ("herosms", "herosms_api") and settings["sms_service"] == "dr"
    reuse, extra_max = _hero_task_reuse_policy(provider_key, settings)

    assert herosms_dr is True
    assert reuse is False
    assert extra_max == 3


def test_non_openai_service_keeps_task_reuse_policy():
    settings = {
        "sms_service": "cursor",
        "register_reuse_phone_to_max": "true",
        "register_phone_extra_max": "3",
    }
    reuse, extra_max = _hero_task_reuse_policy("herosms_api", settings)
    assert reuse is True
    assert extra_max == 3
