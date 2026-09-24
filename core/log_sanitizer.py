"""Helpers for redacting sensitive values before they enter application logs."""
from __future__ import annotations

import builtins
import logging
import re
import traceback
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|pwd|api[_-]?key|secret|client[_-]?secret|private[_-]?key|"
    r"token|auth[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"session(?:[_-]?token)?|csrf(?:[_-]?token)?|device[_-]?token|verification[_-]?token|"
    r"captcha[_-]?token|turnstile[_-]?token|mail[_-]?token|authorization|cookie|"
    r"otp|verification[_-]?code|sms[_-]?code|email[_-]?code|auth[_-]?code|"
    r"proxy[_-]?(?:password|pass)|jwt)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(
    r"(?<![\w.])([A-Za-z0-9+])(?:[A-Za-z0-9._%+-]*)(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])"
)
_PHONE_RE = re.compile(r"(?<![\w])\+?[0-9][0-9 .()\-]{6,}[0-9](?![\w])")
_VERIFICATION_CODE_RE = re.compile(
    r"((?:短信验证码|验证码|verification\s+code|otp)\s*[:=：]?\s*)[0-9]{4,8}",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:['\"]?\b(?:password|passwd|pwd|api[_-]?key|apikey|secret|client[_-]?secret|"
    r"private[_-]?key|token|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|session(?:[_-]?token)?|csrf(?:[_-]?token)?|device[_-]?token|verification[_-]?token|"
    r"captcha[_-]?token|turnstile[_-]?token|mail[_-]?token|authorization|cookie|"
    r"otp|verification[_-]?code|sms[_-]?code|email[_-]?code|auth[_-]?code|"
    r"proxy[_-]?(?:password|pass)|jwt)\b['\"]?)\s*[:=]\s*)"
    r"(?!(?:\[REDACTED\]))(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s]+)"
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def _is_sensitive_key(key: object) -> bool:
    return bool(_SENSITIVE_KEY_RE.fullmatch(str(key).strip()))


def _mask_phone(match: re.Match[str]) -> str:
    value = match.group(0)
    digits = re.sub(r"\D", "", value)
    if len(digits) < 8:
        return value
    prefix_length = max(3, len(digits) - 8)
    masked = f"{digits[:prefix_length]}****{digits[-4:]}"
    return f"+{masked}" if value.startswith("+") else masked


def _sanitize_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return url

        # Rebuild from hostname/port rather than netloc so user-info is discarded.
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = f"{host}:{port}" if port is not None else host

        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            safe_value = REDACTED if _is_sensitive_key(key) else sanitize_text(value)
            query.append((key, safe_value))
        encoded_query = "&".join(
            f"{quote_plus(key)}={quote_plus(value, safe='[]')}" for key, value in query
        )
        return urlunsplit((parts.scheme, netloc, parts.path, encoded_query, parts.fragment))
    except (TypeError, ValueError):
        return REDACTED


def sanitize_text(value: object) -> str:
    """Return a log-safe string without changing the original value."""
    text = "" if value is None else str(value)
    text = _URL_RE.sub(lambda match: _sanitize_url(match.group(0)), text)
    text = _BEARER_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _VERIFICATION_CODE_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _EMAIL_RE.sub(lambda match: f"{match.group(1)}***{match.group(2)}", text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return text


_MAX_SANITIZE_DEPTH = 20


def sanitize_value(
    value: Any,
    *,
    key: object | None = None,
    _seen: frozenset[int] = frozenset(),
    _depth: int = 0,
) -> Any:
    """Recursively return a redacted copy of a structured log value."""
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, (dict, list, tuple)):
        if _depth >= _MAX_SANITIZE_DEPTH or id(value) in _seen:
            return REDACTED
        seen = _seen | {id(value)}
        if isinstance(value, dict):
            return {
                item_key: sanitize_value(item_value, key=item_key, _seen=seen, _depth=_depth + 1)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [sanitize_value(item, _seen=seen, _depth=_depth + 1) for item in value]
        return tuple(sanitize_value(item, _seen=seen, _depth=_depth + 1) for item in value)
    if isinstance(value, BaseException):
        return sanitize_text(value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


_STANDARD_LOG_FIELDS = {
    "name", "levelname", "levelno", "pathname", "filename", "module", "lineno",
    "funcName", "created", "msecs", "relativeCreated", "asctime", "thread",
    "threadName", "processName", "process", "taskName",
}


class SensitiveDataFilter(logging.Filter):
    """Redact log message, arguments, exception details, and extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = sanitize_text(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = sanitize_text("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = sanitize_text(record.exc_text)
            for key, value in list(record.__dict__.items()):
                if key not in {"msg", "args", "exc_info", "exc_text"}:
                    record.__dict__[key] = sanitize_value(value, key=key)
        except Exception:
            record.msg = REDACTED
            record.args = ()
            record.exc_info = None
            record.exc_text = REDACTED
            for key in list(record.__dict__):
                if key not in {"msg", "args", "exc_info", "exc_text", *_STANDARD_LOG_FIELDS}:
                    record.__dict__[key] = REDACTED
        return True


def install_log_sanitizer() -> None:
    """Attach one redaction filter to all handlers currently registered."""
    loggers = [logging.getLogger()]
    loggers.extend(
        logger for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    handlers = [handler for logger in loggers for handler in logger.handlers]
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    seen_handlers: set[int] = set()
    for handler in handlers:
        if id(handler) in seen_handlers:
            continue
        seen_handlers.add(id(handler))
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())


def safe_print(*args: object, **kwargs: Any) -> None:
    """Print only redacted representations of the supplied arguments."""
    safe_args = []
    for arg in args:
        if isinstance(arg, (dict, list, tuple, BaseException)):
            safe_args.append(sanitize_value(arg))
        else:
            safe_args.append(sanitize_text(arg))
    builtins.print(*safe_args, **kwargs)
