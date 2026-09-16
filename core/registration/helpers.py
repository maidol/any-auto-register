from __future__ import annotations

from typing import Any

from core.base_sms import create_phone_callbacks

from .errors import BrowserReuseRequiredError, IdentityResolutionError, RegistrationUnsupportedError
from .models import RegistrationContext


def has_reusable_oauth_browser(identity: Any) -> bool:
    return bool((getattr(identity, "chrome_user_data_dir", "") or "").strip() or (getattr(identity, "chrome_cdp_url", "") or "").strip())


def resolve_timeout(extra: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return int(value)
    return int(default)


def ensure_identity_email(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "email", ""):
        raise IdentityResolutionError(message)


def ensure_mailbox_identity(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "has_mailbox", False):
        raise IdentityResolutionError(message)


def ensure_oauth_executor_allowed(ctx: RegistrationContext, allowed_executor_types: tuple[str, ...] | None, message: str | None = None) -> None:
    if not allowed_executor_types:
        return
    if ctx.executor_type not in allowed_executor_types:
        expected = ", ".join(allowed_executor_types)
        raise RegistrationUnsupportedError(message or f"{ctx.platform_display_name} 当前 OAuth 仅支持 executor_type={expected}")


def ensure_oauth_browser_reuse(ctx: RegistrationContext, message: str) -> None:
    if not has_reusable_oauth_browser(ctx.identity):
        raise BrowserReuseRequiredError(message)


def build_otp_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    code_pattern: str | None = None,
    wait_message: str = "等待验证码...",
    success_label: str = "验证码",
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    # identity.before_ids 是 identity 解析时取的一次快照，只能当“整轮注册开始前”的
    # 地板。一轮注册会要多次码（重发、OAuth 再登录），第二次等待时上一封 OTP 邮件
    # 仍在收件箱里、且不在这个地板里，于是被当成新邮件——回放旧验证码。
    # 每消费掉一封就把地板抬到“此刻收件箱里的全部邮件”。抬的时机在拿到码之后，
    # 所以不会像“等待前才快照”那样把已经躺在收件箱里的目标邮件一起挡掉。
    floor_ids = {"value": set(getattr(ctx.identity, "before_ids", set()) or set())}

    def _snapshot_ids() -> set:
        try:
            return set(mailbox.get_current_ids(mail_acct) or set())
        except Exception as exc:
            ctx.log(f"刷新收件箱地板失败: {exc}")
            return set()

    def otp_cb():
        ctx.log(wait_message)
        # 等待前的快照只作为成功后的地板下限，不能作为 before_ids 传入。
        pre_ids = _snapshot_ids()
        kwargs = {"keyword": keyword, "before_ids": set(floor_ids["value"])}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if code_pattern:
            kwargs["code_pattern"] = code_pattern
        code = mailbox.wait_for_code(mail_acct, **kwargs)
        if code:
            ctx.log(f"{success_label}: {code}")
            # 只升不降：快照失败时返回空集，不能清空已有地板。
            floor_ids["value"] |= pre_ids | _snapshot_ids()
        return code

    return otp_cb


def build_phone_callbacks(ctx: RegistrationContext, *, service: str | None = None):
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    extra = ctx.extra
    requested_provider_key = str(
        extra.get("sms_provider")
        or extra.get("phone_provider")
        or ""
    ).strip()
    settings_repo = ProviderSettingsRepository()
    definitions_repo = ProviderDefinitionsRepository()

    provider_key = requested_provider_key
    source = "task params"
    if not provider_key:
        provider_key = str(settings_repo.get_default_provider_key("sms") or "").strip()
        source = "global default"
    if not provider_key:
        if extra.get("sms_activate_api_key"):
            provider_key = "sms_activate"
            source = "legacy sms_activate_api_key"
    if not provider_key:
        ctx.log("[SMS] 未配置 SMS provider（任务参数/全局默认/历史兼容字段都为空），phone_callback=None — 注册到 add_phone 步骤将抛错")
        return None, None

    definition = definitions_repo.get_by_key("sms", provider_key)
    merged = settings_repo.resolve_runtime_settings("sms", provider_key, extra) if definition else dict(extra)

    auth_fields = []
    if definition:
        auth_fields = [
            str(field.get("key") or "").strip()
            for field in definition.get_fields()
            if str(field.get("category") or "").strip() == "auth"
        ]
    if auth_fields and not any(str(merged.get(field_key, "")).strip() for field_key in auth_fields):
        ctx.log(f"[SMS] provider={provider_key} (来源={source}) 已找到 definition，但认证字段 {auth_fields} 全部为空，phone_callback=None")
        return None, None

    if ctx.proxy and not str(merged.get("sms_proxy") or merged.get("proxy") or "").strip():
        merged["sms_proxy"] = ctx.proxy

    country = str(
        merged.get("sms_country")
        or merged.get("phone_country")
        or merged.get("sms_activate_country")
        or merged.get("sms_activate_default_country")
        or merged.get("herosms_country")
        or merged.get("herosms_default_country")
        or merged.get("smsbower_country")
        or merged.get("smsbower_default_country")
        or ""
    ).strip()
    sms_service = str(
        merged.get("sms_service")
        or merged.get("herosms_service")
        or merged.get("herosms_default_service")
        or merged.get("smsbower_service")
        or merged.get("smsbower_default_service")
        or merged.get("sms_activate_service")
        or merged.get("sms_activate_default_service")
        or service
        or ctx.platform_name
    ).strip() or ctx.platform_name
    ctx.log(f"[SMS] phone_callback 已就绪: provider={provider_key} 来源={source} service={sms_service} country={country or 'default'}")
    return create_phone_callbacks(
        provider_key,
        merged,
        service=sms_service,
        country=country,
        log_fn=ctx.log,
    )


def build_link_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    wait_message: str = "等待验证链接邮件...",
    success_label: str = "验证链接",
    preview_chars: int = 80,
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    def link_cb():
        ctx.log(wait_message)
        before_ids = mailbox.get_current_ids(mail_acct)
        kwargs = {"keyword": keyword, "before_ids": before_ids}
        if timeout is not None:
            kwargs["timeout"] = timeout
        link = mailbox.wait_for_link(mail_acct, **kwargs)
        if link:
            preview = link if len(link) <= preview_chars else f"{link[:preview_chars]}..."
            ctx.log(f"{success_label}: {preview}")
        return link

    return link_cb
