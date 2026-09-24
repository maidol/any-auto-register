import logging

import pytest

from core.log_sanitizer import (
    REDACTED,
    SensitiveDataFilter,
    install_log_sanitizer,
    safe_print,
    sanitize_text,
    sanitize_value,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Contact u@example.com or +8613812345678", "Contact u***@example.com or +86138****5678"),
        ("short code 12345", "short code 12345"),
        ("password=hunter2", f"password={REDACTED}"),
        ("api_key: fake-key-123", f"api_key: {REDACTED}"),
        ("Bearer fake-token-456", f"Bearer {REDACTED}"),
        ("验证码 246810", f"验证码 {REDACTED}"),
        ("短信验证码：135790", f"短信验证码：{REDACTED}"),
        (
            "https://user:pass@example.test/path?token=secret&view=compact",
            f"https://example.test/path?token={REDACTED}&view=compact",
        ),
    ],
)
def test_sanitize_text_redacts_sensitive_text(source, expected):
    assert sanitize_text(source) == expected
    assert sanitize_text(sanitize_text(source)) == expected


@pytest.mark.parametrize(
    "key",
    [
        "password", "passwd", "pwd", "api_key", "apikey", "secret", "token",
        "access_token", "refresh_token", "id_token", "session", "session_token",
        "authorization", "cookie", "otp", "verification_code", "sms_code",
        "email_code", "auth_code", "proxy_password", "proxy_pass",
    ],
)
def test_sanitize_value_redacts_sensitive_mapping_keys_case_insensitively(key):
    original = {key.upper(): {"nested": ["sensitive"]}, "ordinary": "u@example.com"}
    result = sanitize_value(original)

    assert result[key.upper()] == REDACTED
    assert result["ordinary"] == "u***@example.com"
    assert original[key.upper()] == {"nested": ["sensitive"]}


def test_sanitize_value_recurses_without_mutation_and_preserves_tuple_and_exceptions():
    original = {
        "items": ["mail u@example.com", ("call +8613812345678",)],
        "failure": ValueError("password=unsafe-value"),
    }
    result = sanitize_value(original)

    assert result == {
        "items": ["mail u***@example.com", ("call +86138****5678",)],
        "failure": "password=[REDACTED]",
    }
    assert isinstance(result["items"][1], tuple)
    assert "unsafe-value" in str(original["failure"])


def test_sanitize_value_preserves_non_sensitive_url_query_values():
    value = {"endpoint": "https://user:pass@example.test/?view=compact&access_token=opaque"}
    result = sanitize_value(value)
    assert result["endpoint"] == f"https://example.test/?view=compact&access_token={REDACTED}"


def test_log_filter_sanitizes_formatted_arguments_and_traceback(caplog):
    logger = logging.getLogger("tests.log_sanitizer.filter")
    logger.handlers.clear()
    logger.propagate = True
    logger.setLevel(logging.ERROR)
    logger.addFilter(SensitiveDataFilter())

    try:
        raise RuntimeError("api_key=trace-secret-123")
    except RuntimeError:
        with caplog.at_level(logging.ERROR, logger=logger.name):
            logger.error("password=%s contact=%s", "format-secret-456", "u@example.com", exc_info=True)

    rendered = caplog.text
    assert "trace-secret-123" not in rendered
    assert "format-secret-456" not in rendered
    assert "u@example.com" not in rendered
    assert "[REDACTED]" in rendered


def test_filter_fails_closed_if_sanitization_raises(monkeypatch):
    import core.log_sanitizer as sanitizer

    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "password=unsafe", (), None)
    monkeypatch.setattr(sanitizer, "sanitize_value", lambda value: (_ for _ in ()).throw(RuntimeError()))
    assert SensitiveDataFilter().filter(record) is True
    assert record.msg == REDACTED
    assert record.args == ()


def test_install_log_sanitizer_covers_existing_handlers_and_is_idempotent():
    logger = logging.getLogger("tests.log_sanitizer.install")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    root_handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(root_handler)
    try:
        install_log_sanitizer()
        first_count = len(handler.filters)
        root_first_count = len(root_handler.filters)
        install_log_sanitizer()
        assert first_count >= 1
        assert root_first_count >= 1
        assert len(handler.filters) == first_count
        assert len(root_handler.filters) == root_first_count
    finally:
        logger.removeHandler(handler)
        root.removeHandler(root_handler)


def test_safe_print_sanitizes_arguments(monkeypatch):
    import builtins

    output = []
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: output.append(args))
    safe_print("password=unsafe", "u@example.com")
    safe_print({"password": "unsafe", "email": "u@example.com"})
    assert output == [
        (f"password={REDACTED}", "u***@example.com"),
        ({"password": REDACTED, "email": "u***@example.com"},),
    ]


def test_sanitize_text_handles_plus_address_and_encoded_query_secret():
    value = sanitize_text("contact +alias@example.com https://example.test/?next=password%3Dlive-secret")

    assert "+alias@example.com" not in value
    assert "live-secret" not in value


def test_sanitize_text_handles_escaped_and_delimiter_credentials():
    value = sanitize_text('{"password":"ab\\"cd"} password=abc]def password=abc;def')

    assert '"ab' not in value
    assert "cd" not in value
    assert "abc]def" not in value
    assert "abc;def" not in value


def test_sanitize_text_and_value_cover_common_credential_keys():
    assert "live-csrf" not in sanitize_text("csrf_token=live-csrf")
    assert "live-device" not in sanitize_text("device_token=live-device")
    assert "live-secret" not in sanitize_text("client_secret=live-secret")
    assert sanitize_value({"csrf_token": "live-csrf", "client_secret": "live-secret"}) == {
        "csrf_token": REDACTED,
        "client_secret": REDACTED,
    }


def test_sanitize_text_fails_closed_for_malformed_url():
    assert "[::1" not in sanitize_text("response=https://[::1")


def test_sanitize_value_handles_cycles():
    value = {}
    value["self"] = value

    assert sanitize_value(value)["self"] == REDACTED


def test_filter_sanitizes_preformatted_exception_and_extra_fields(monkeypatch):
    import core.log_sanitizer as sanitizer

    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "safe", (), None)
    record.exc_text = "password=preformatted-secret"
    record.secret = "extra-secret"
    monkeypatch.setattr(sanitizer, "sanitize_value", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))

    SensitiveDataFilter().filter(record)

    assert record.exc_text == REDACTED
    assert record.secret == REDACTED


def test_install_log_sanitizer_covers_last_resort_handler():
    install_log_sanitizer()

    assert logging.lastResort is None or any(
        isinstance(item, SensitiveDataFilter) for item in logging.lastResort.filters
    )
