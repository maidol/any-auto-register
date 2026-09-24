from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "core/base_mailbox.py",
    "core/base_sms.py",
    "platforms/blink/core.py",
    "platforms/blink/protocol_mailbox.py",
    "platforms/chatgpt/register.py",
    "platforms/cerebras/protocol_mailbox.py",
    "platforms/cursor/browser_register.py",
    "platforms/cursor/protocol_mailbox.py",
    "platforms/grok/core.py",
    "platforms/kiro/browser_register.py",
    "platforms/kiro/core.py",
    "platforms/kiro/protocol_mailbox.py",
    "platforms/openblocklabs/browser_register.py",
    "platforms/tavily/protocol_mailbox.py",
    "platforms/trae/browser_register.py",
    "platforms/trae/core.py",
    "platforms/trae/protocol_mailbox.py",
    "platforms/windsurf/browser_register.py",
    "platforms/windsurf/protocol_mailbox.py",
    "services/turnstile_solver/api_solver.py",
)
FORBIDDEN_SNIPPETS = (
    "生成密码[{index}/{len(candidates)}]: {password}",
    "注册凭据: {email} / {password}",
    "验证码: {otp}",
    "验证码: {code}",
    "填写验证码: {code}",
    "收到验证码: {code}",
    "API Key: {api_key[:20]}",
    "magic_token={token[:16]}",
    "注入 Turnstile token ({token[:40]}...)",
    "Solver 返回 token: {token[:50]}",
    "CSRF token: {csrf_token[:20]}",
    "session-token: {session_token[:30]}",
    "signupCsrfToken={signup_token[:12]}",
    "awsd2c-token: {token[:60]}",
    "bearer token (sessionToken)={bearer_token[:60]}",
    "accessToken={access_token[:60]}",
    "device_token={device_token[:60]}",
    "refreshToken={refresh_token[:60]}",
    "Turnstile: {token[:40]}",
    "Successfully solved captcha - {COLORS.get('MAGENTA')}{token[:10]}",
    "Successfully solved captcha - {COLORS.get('MAGENTA')}{element_token[:10]}",
    "提交注册... otp={otp}",
)


def test_sensitive_values_are_not_explicitly_previewed_in_logs():
    source = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in SOURCE_FILES)
    for snippet in FORBIDDEN_SNIPPETS:
        assert snippet not in source, snippet
