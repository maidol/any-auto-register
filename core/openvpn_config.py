"""Strict parsing and normalization for Mihomo OpenVPN proxy entries."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any



MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_OPENVPN_ENTRIES = 500
MAX_FIELD_LENGTH = 512 * 1024

_SUPPORTED_CIPHERS = {
    "AES-128-GCM",
    "AES-192-GCM",
    "AES-256-GCM",
    "AES-128-CBC",
    "AES-192-CBC",
    "AES-256-CBC",
    "CHACHA20-POLY1305",
}
_SUPPORTED_AUTHS = {"MD5", "SHA1", "SHA256", "SHA384", "SHA512"}
_BLOCK_FIELDS = {"ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2"}
@dataclass(slots=True)
class OpenVPNImportResult:
    entries: list[dict[str, object]]
    failures: list[dict[str, str]]


def _normalize_cipher(value: object) -> str:
    normalized = str(value).upper().replace(" ", "").replace("_", "-")
    if normalized == "AES-CBC":
        normalized = "AES-128-CBC"
    if normalized not in _SUPPORTED_CIPHERS:
        raise ValueError(f"unsupported Mihomo OpenVPN cipher: {value}")
    return normalized


def _normalize_auth(value: object) -> str:
    normalized = str(value).upper().replace(" ", "").replace("-", "")
    if normalized not in _SUPPORTED_AUTHS:
        raise ValueError(f"unsupported Mihomo OpenVPN auth: {value}")
    return normalized


def _normalize_proto(value: object) -> str:
    normalized = str(value or "udp").lower()
    if normalized in {"udp", "udp4", "udp6"}:
        return "udp"
    if normalized in {"tcp", "tcp-client", "tcp4", "tcp6"}:
        return "tcp"
    raise ValueError(f"unsupported Mihomo OpenVPN proto: {value}")


def _split_values(value: object) -> list[str]:
    if isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        values = [part for part in re.split(r"[,:\s]+", str(value).strip())]
    return [item for item in values if item]


def _validate_pem(value: object, field: str) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_FIELD_LENGTH:
        raise ValueError(f"OpenVPN {field} exceeds size limit")
    lines = text.splitlines()
    if len(lines) < 3 or not lines[0].startswith("-----BEGIN "):
        raise ValueError(f"OpenVPN {field} is not PEM")
    match = re.fullmatch(r"-----BEGIN ([^-]+)-----", lines[0])
    if not match or lines[-1] != f"-----END {match.group(1)}-----":
        raise ValueError(f"OpenVPN {field} is not PEM")
    body = "".join(lines[1:-1])
    try:
        decoded = base64.b64decode(body.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError(f"OpenVPN {field} is not PEM") from exc
    if not decoded:
        raise ValueError(f"OpenVPN {field} is not PEM")
    return text


def _validate_static_key(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"OpenVPN {field} is empty")
    if len(text) > MAX_FIELD_LENGTH:
        raise ValueError(f"OpenVPN {field} exceeds size limit")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if field in {"tls-auth", "tls-crypt"}:
        begin = "-----BEGIN OpenVPN Static key V1-----"
        end = "-----END OpenVPN Static key V1-----"
        if begin not in lines or end not in lines:
            raise ValueError(f"OpenVPN {field} is not a valid static key")
        start = lines.index(begin) + 1
        stop = lines.index(end)
        body = "".join(lines[start:stop])
        if not re.fullmatch(r"[0-9a-fA-F]{512}", body):
            raise ValueError(f"OpenVPN {field} is not a valid static key")
    elif field == "tls-crypt-v2":
        begin = "-----BEGIN OpenVPN tls-crypt-v2 client key-----"
        end = "-----END OpenVPN tls-crypt-v2 client key-----"
        if begin not in lines or end not in lines or lines.index(begin) + 1 >= lines.index(end):
            raise ValueError(f"OpenVPN {field} is not a valid static key")
    return text


def _validate_scalar_int(entry: dict[str, object], field: str) -> None:
    if field not in entry:
        return
    try:
        value = int(entry[field])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"OpenVPN {field} must be an integer") from exc
    if value < 0:
        raise ValueError(f"OpenVPN {field} must not be negative")
    entry[field] = value


def validate_openvpn_entry(entry: dict[str, object]) -> dict[str, object]:
    """Return a normalized, Mihomo-supported OpenVPN mapping."""
    if not isinstance(entry, dict):
        raise ValueError("OpenVPN proxy entry must be an object")
    if str(entry.get("type") or "").lower() != "openvpn":
        raise ValueError(f"unsupported proxy type: {entry.get('type', '')}")
    for field in ("dialer-proxy", "dns"):
        if entry.get(field) not in (None, "", []):
            raise ValueError(f"unsupported runtime field: {field}")

    name = str(entry.get("name") or "").strip()
    server = str(entry.get("server") or "").strip()
    if not name:
        raise ValueError("OpenVPN name is required")
    if not server:
        raise ValueError("OpenVPN server is required")
    if len(name) > 200 or len(server) > 255:
        raise ValueError("OpenVPN name or server is too long")
    try:
        port = int(entry.get("port", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenVPN port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("OpenVPN port is out of range")

    normalized: dict[str, object] = {
        "name": name,
        "type": "openvpn",
        "server": server,
        "port": port,
        "proto": _normalize_proto(entry.get("proto", "udp")),
        "udp": _normalize_proto(entry.get("proto", "udp")) == "udp",
        "ca": _validate_pem(entry.get("ca"), "ca"),
    }

    cert = entry.get("cert")
    key = entry.get("key")
    if bool(cert) != bool(key):
        raise ValueError("OpenVPN cert and key must be provided together")
    username = str(entry.get("username") or "").strip()
    password = str(entry.get("password") or "")
    if cert:
        normalized["cert"] = _validate_pem(cert, "cert")
        normalized["key"] = _validate_pem(key, "key")
    elif not username or not password.strip():
        raise ValueError("OpenVPN profile needs cert/key or non-empty username and password")
    else:
        normalized["username"] = username
        normalized["password"] = password

    tls_fields = [field for field in ("tls-auth", "tls-crypt", "tls-crypt-v2") if entry.get(field)]
    if len(tls_fields) > 1:
        raise ValueError("OpenVPN TLS key fields are mutually exclusive")
    for field in tls_fields:
        normalized[field] = _validate_static_key(entry[field], field)
    key_direction = str(entry.get("key-direction") or "")
    if key_direction not in {"", "0", "1"}:
        raise ValueError(f"unsupported Mihomo OpenVPN key-direction: {key_direction}")
    if key_direction and "tls-auth" not in normalized:
        raise ValueError("OpenVPN key-direction requires tls-auth")
    if key_direction:
        normalized["key-direction"] = key_direction

    if "cipher" in entry and entry["cipher"] not in (None, ""):
        normalized["cipher"] = _normalize_cipher(entry["cipher"])
    if "data-ciphers" in entry and entry["data-ciphers"] not in (None, "", []):
        normalized["data-ciphers"] = [_normalize_cipher(item) for item in _split_values(entry["data-ciphers"])]
    if "data-ciphers-fallback" in entry and entry["data-ciphers-fallback"] not in (None, ""):
        normalized["data-ciphers-fallback"] = _normalize_cipher(entry["data-ciphers-fallback"])
    if "auth" in entry and entry["auth"] not in (None, ""):
        normalized["auth"] = _normalize_auth(entry["auth"])
    if "comp-lzo" in entry and entry["comp-lzo"] not in (None, ""):
        comp_lzo = str(entry["comp-lzo"]).lower()
        if comp_lzo not in {"yes", "no", "adaptive"}:
            raise ValueError(f"unsupported Mihomo OpenVPN comp-lzo: {comp_lzo}")
        normalized["comp-lzo"] = comp_lzo

    for field in ("ping", "ping-restart", "tran-window", "handshake-timeout", "mtu"):
        _validate_scalar_int(entry, field)
        if field in entry:
            normalized[field] = entry[field]
    if "dev" in entry and str(entry["dev"]).lower() != "tun":
        raise ValueError("Only TUN OpenVPN devices are accepted")
    if "dev" in entry:
        normalized["dev"] = "tun"
    for field in ("peer-info", "dialer-proxy", "dns"):
        if field in entry and entry[field] not in (None, "", []):
            normalized[field] = entry[field]
    if "remote-dns-resolve" in entry:
        normalized["remote-dns-resolve"] = bool(entry["remote-dns-resolve"])
    return normalized


def parse_mihomo_openvpn_yaml(document: object) -> OpenVPNImportResult:
    if isinstance(document, str):
        if len(document.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("Mihomo YAML exceeds the import size limit")
        import yaml

        try:
            document = yaml.safe_load(document)
        except yaml.YAMLError as exc:
            raise ValueError("Malformed Mihomo YAML") from exc
    if not isinstance(document, dict):
        raise ValueError("Mihomo YAML root must be an object")
    raw_entries = document.get("proxies")
    if not isinstance(raw_entries, list):
        raise ValueError("Mihomo YAML proxies must be a list")
    if len(raw_entries) > MAX_OPENVPN_ENTRIES:
        raise ValueError("Mihomo YAML contains too many proxies")

    entries: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    seen: set[tuple[str, str, int]] = set()
    seen_names: set[str] = set()
    for row_number, raw_entry in enumerate(raw_entries, start=1):
        try:
            normalized = validate_openvpn_entry(raw_entry)
            identity = (str(normalized["name"]), str(normalized["server"]), int(normalized["port"]))
            name = str(normalized["name"])
            if name in seen_names:
                raise ValueError("duplicate OpenVPN name")
            if identity in seen:
                raise ValueError("duplicate OpenVPN identity")
            seen_names.add(name)
            seen.add(identity)
            entries.append(normalized)
        except (TypeError, ValueError, KeyError) as exc:
            failures.append({"row_number": str(row_number), "reason": str(exc)})
    return OpenVPNImportResult(entries=entries, failures=failures)


def render_single_openvpn_config(
    entries: list[dict[str, object]], *, mixed_port: int, controller: str
) -> str:
    import yaml

    document = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": controller,
        "proxies": entries,
        "proxy-groups": [{"name": "VPNGate", "type": "select", "proxies": [entry["name"] for entry in entries]}],
        "rules": ["MATCH,VPNGate"],
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)


def config_json(entry: dict[str, object]) -> str:
    """Serialize normalized config for the OpenVPN repository."""
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
