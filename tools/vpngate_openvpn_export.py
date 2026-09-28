#!/usr/bin/env python3
"""Export VPN Gate OpenVPN nodes as a Mihomo proxy configuration.

Run manually from the repository root::

    python tools/vpngate_openvpn_export.py --output-dir ./vpngate-output

The command writes the untouched VPN Gate snapshot to ``vpngate.csv``, a
conversion audit to ``vpngate.meta.json``, and Mihomo's OpenVPN proxy entries
to ``mihomo-openvpn.yaml``.  It does not start OpenVPN or modify the app's
proxy database.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.openvpn_config import validate_openvpn_entry


DEFAULT_SOURCE = "https://www.vpngate.net/api/iphone/"
DEFAULT_TIMEOUT = 30
MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024
MAX_PROFILE_BYTES = 128 * 1024

_REQUIRED_COLUMNS = {
    "HostName",
    "IP",
    "CountryLong",
    "CountryShort",
    "OpenVPN_ConfigData_Base64",
}
_INLINE_BLOCKS = {
    "ca",
    "cert",
    "key",
    "tls-auth",
    "tls-crypt",
    "tls-crypt-v2",
}
_SAFE_DIRECTIVES = {
    "auth",
    "auth-user-pass",
    "cipher",
    "client",
    "comp-lzo",
    "compress",
    "connect-retry",
    "connect-retry-max",
    "connect-timeout",
    "data-ciphers",
    "data-ciphers-fallback",
    "dev",
    "dev-type",
    "dhcp-option",
    "explicit-exit-notify",
    "keepalive",
    "key-direction",
    "mute",
    "nobind",
    "persist-key",
    "persist-tun",
    "ping",
    "ping-restart",
    "ping-timer-rem",
    "proto",
    "pull",
    "rcvbuf",
    "remote",
    "remote-cert-tls",
    "remote-random",
    "remote-random-hostname",
    "reneg-sec",
    "resolv-retry",
    "route-delay",
    "sndbuf",
    "tls-auth",
    "tls-client",
    "tls-cipher",
    "tls-ciphersuites",
    "tls-version-min",
    "verb",
    "verify-x509-name",
}
_INT_FIELDS = {
    "ping",
    "ping-restart",
    "tran-window",
    "handshake-timeout",
    "mtu",
}
_STRING_FIELDS = {"key-direction", "dev"}
_MIHOMO_CIPHERS = {
    "AES-128-GCM",
    "AES-192-GCM",
    "AES-256-GCM",
    "AES-128-CBC",
    "AES-192-CBC",
    "AES-256-CBC",
    "CHACHA20-POLY1305",
}
_MIHOMO_AUTHS = {"MD5", "SHA1", "SHA256", "SHA384", "SHA512"}


def fetch_snapshot(
    source: str,
    timeout: int,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
) -> str:
    """Fetch a bounded UTF-8 snapshot from a VPN Gate feed."""
    if int(max_bytes) <= 0:
        raise ValueError("VPN Gate snapshot byte limit must be positive")
    request = urllib.request.Request(
        source,
        headers={
            "User-Agent": "any-auto-register-vpngate-export/1.0",
            "Accept": "text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise RuntimeError(f"VPN Gate returned HTTP {status}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > int(max_bytes):
                raise ValueError("VPN Gate snapshot exceeds the configured limit")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="strict")


def _decode_profile(encoded: str) -> str:
    compact = "".join(str(encoded or "").split())
    if not compact:
        raise ValueError("OpenVPN configuration is empty")
    try:
        raw = base64.b64decode(compact.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("OpenVPN configuration is not valid base64") from exc
    if not raw or len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("OpenVPN configuration size is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("OpenVPN configuration is not valid UTF-8") from exc
    if "\x00" in text:
        raise ValueError("OpenVPN configuration contains a NUL byte")
    return text


def _parse_int(value: str, field: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{field} must not be negative")
    return result


def _split_cipher_list(value: str) -> list[str]:
    return [part for part in re.split(r"[,:\s]+", value.strip()) if part]


def _normalize_cipher(value: str) -> str:
    normalized = value.upper().replace(" ", "").replace("_", "-")
    if normalized == "AES-CBC":
        normalized = "AES-128-CBC"
    if normalized not in _MIHOMO_CIPHERS:
        raise ValueError(f"unsupported Mihomo OpenVPN cipher: {value}")
    return normalized


def _normalize_auth(value: str) -> str:
    normalized = value.upper().replace(" ", "").replace("-", "")
    if normalized not in _MIHOMO_AUTHS:
        raise ValueError(f"unsupported Mihomo OpenVPN auth: {value}")
    return normalized


def _validate_pem(value: object, field: str) -> str:
    text = str(value or "").strip()
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


def parse_openvpn_profile(config_text: str) -> dict[str, object]:
    """Parse the safe, Mihomo-relevant subset of an OpenVPN profile."""
    if not config_text or len(config_text.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("OpenVPN configuration size is invalid")
    if "\x00" in config_text:
        raise ValueError("OpenVPN configuration contains a NUL byte")

    profile: dict[str, object] = {}
    directives: set[str] = set()
    current_block: str | None = None
    block_lines: list[str] = []
    remote_seen = False
    remote_proto_seen = False

    def finish_block() -> None:
        nonlocal current_block, block_lines
        if current_block is None:
            return
        content = "\n".join(block_lines).strip()
        if not content:
            raise ValueError(f"OpenVPN {current_block} block is empty")
        profile[current_block] = content
        current_block = None
        block_lines = []

    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if current_block is not None:
            if line.lower() == f"</{current_block}>":
                finish_block()
            elif line.startswith("<") or line.startswith("</"):
                raise ValueError(f"Unexpected nested OpenVPN block: {line}")
            else:
                block_lines.append(raw_line.rstrip())
            continue
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("<") and line.endswith(">"):
            tag = line[1:-1].strip().lower()
            if tag not in _INLINE_BLOCKS:
                raise ValueError(f"Unsupported OpenVPN inline block: {tag}")
            current_block = tag
            directives.add(tag)
            continue

        parts = re.split(r"\s+", line)
        directive = parts[0].lstrip("-").lower()
        if directive not in _SAFE_DIRECTIVES:
            raise ValueError(f"Unsupported OpenVPN directive: {directive}")
        directives.add(directive)

        if directive == "tls-auth":
            raise ValueError("external tls-auth file is not supported")
        if directive == "client":
            profile["client"] = True
        elif directive == "remote":
            if remote_seen:
                raise ValueError("multiple OpenVPN remote directives are not supported")
            if len(parts) < 3:
                raise ValueError("OpenVPN remote directive is incomplete")
            try:
                port = int(parts[2])
            except ValueError as exc:
                raise ValueError("OpenVPN remote port is invalid") from exc
            if not 1 <= port <= 65535:
                raise ValueError("OpenVPN remote port is out of range")
            remote_proto = parts[3].lower() if len(parts) >= 4 else None
            profile["server"] = parts[1]
            profile["port"] = port
            if remote_proto:
                profile["proto"] = _normalize_proto(remote_proto)
                remote_proto_seen = True
            remote_seen = True
        elif directive == "proto":
            if not remote_proto_seen:
                profile["proto"] = _normalize_proto(parts[1] if len(parts) > 1 else "")
        elif directive in {"data-ciphers", "data-ciphers-fallback"}:
            values = _split_cipher_list(" ".join(parts[1:]))
            if not values:
                raise ValueError(f"{directive} is empty")
            profile[directive] = values if directive == "data-ciphers" else values[0]
        elif directive in _INT_FIELDS:
            if len(parts) < 2:
                raise ValueError(f"{directive} is incomplete")
            profile[directive] = _parse_int(parts[1], directive)
        elif directive in {"cipher", "auth"}:
            if len(parts) < 2:
                raise ValueError(f"{directive} is incomplete")
            profile[directive] = " ".join(parts[1:])
        elif directive == "comp-lzo":
            if len(parts) < 2 or parts[1].lower() not in {"yes", "no", "adaptive"}:
                raise ValueError("comp-lzo cannot be converted to Mihomo")
            profile[directive] = parts[1].lower()
        elif directive == "compress":
            raise ValueError("compress cannot be converted to Mihomo")
        elif directive in _STRING_FIELDS:
            if len(parts) >= 2:
                profile[directive] = " ".join(parts[1:])
        elif directive == "dev-type":
            if len(parts) >= 2 and parts[1].lower() != "tun":
                raise ValueError("Only TUN OpenVPN devices are accepted")
        elif directive == "auth-user-pass":
            profile["auth-user-pass"] = True

    if current_block is not None:
        raise ValueError(f"Unclosed OpenVPN inline block: {current_block}")
    if "client" not in directives or not remote_seen:
        raise ValueError("OpenVPN client or remote directive is missing")
    if "ca" not in profile:
        raise ValueError("OpenVPN CA certificate is missing")
    if "dev" in profile and str(profile["dev"]).lower() != "tun":
        raise ValueError("Only TUN OpenVPN devices are accepted")
    profile.setdefault("proto", "udp")
    profile["udp"] = profile["proto"] == "udp"
    return profile


def _normalize_proto(value: str) -> str:
    normalized = value.lower()
    if normalized in {"udp", "udp4", "udp6"}:
        return "udp"
    if normalized in {"tcp", "tcp-client", "tcp4", "tcp6"}:
        return "tcp"
    raise ValueError(f"Unsupported OpenVPN protocol: {value}")


def _row_failure(row_number: int, row: dict[str, str], reason: str) -> dict[str, str]:
    return {
        "row_number": str(row_number),
        "host_name": row.get("HostName", ""),
        "ip": row.get("IP", ""),
        "reason": reason,
    }


def parse_snapshot(text: str) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Parse all rows, retaining valid profiles and reporting row failures."""
    if not text or len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("VPN Gate snapshot size is invalid")
    lines = [line for line in text.splitlines() if line and not line.startswith("*")]
    if not lines:
        raise ValueError("VPN Gate snapshot is empty")
    if lines[0].startswith("#"):
        lines[0] = lines[0][1:]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    fields = set(reader.fieldnames or [])
    missing = _REQUIRED_COLUMNS - fields
    if missing:
        raise ValueError(f"VPN Gate snapshot columns are incomplete: {', '.join(sorted(missing))}")

    rows: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    seen_ips: set[str] = set()
    for row_number, raw_row in enumerate(reader, start=2):
        row = {str(key): str(value or "") for key, value in raw_row.items() if key is not None}
        if not row.get("IP") or not row.get("OpenVPN_ConfigData_Base64"):
            failures.append(_row_failure(row_number, row, "IP or OpenVPN configuration is missing"))
            continue
        if row["IP"] in seen_ips:
            failures.append(_row_failure(row_number, row, "duplicate IP"))
            continue
        try:
            profile = parse_openvpn_profile(_decode_profile(row["OpenVPN_ConfigData_Base64"]))
        except (ValueError, UnicodeError) as exc:
            failures.append(_row_failure(row_number, row, str(exc)))
            continue
        row["row_number"] = row_number
        row["profile"] = profile
        rows.append(row)
        seen_ips.add(row["IP"])
    return rows, failures


def _slug(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return re.sub(r"-+", "-", text) or "unknown"


def build_mihomo_proxy(
    row: dict[str, object],
    username: str | None = None,
    password: str | None = None,
) -> dict[str, object]:
    """Convert one parsed VPN Gate row to Mihomo's OpenVPN mapping."""
    profile = row.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("parsed row has no OpenVPN profile")
    ca = _validate_pem(profile.get("ca"), "ca")
    cert = str(profile.get("cert") or "")
    key = str(profile.get("key") or "")
    has_credentials = username is not None and password is not None
    if bool(cert) != bool(key):
        raise ValueError("OpenVPN cert and key must be provided together")
    if cert:
        cert = _validate_pem(cert, "cert")
        key = _validate_pem(key, "key")
    else:
        if not has_credentials or not str(username or "").strip():
            raise ValueError("OpenVPN profile needs cert/key or a non-empty username")
    key_direction = str(profile.get("key-direction") or "")
    if key_direction not in {"", "0", "1"}:
        raise ValueError(f"unsupported Mihomo OpenVPN key-direction: {key_direction}")
    tls_fields = [field for field in ("tls-auth", "tls-crypt", "tls-crypt-v2") if profile.get(field)]
    if len(tls_fields) > 1:
        raise ValueError("OpenVPN TLS key fields are mutually exclusive")
    if profile.get("key-direction") and profile.get("tls-auth") is None:
        raise ValueError("OpenVPN key-direction requires tls-auth")

    country = row.get("CountryShort") or "XX"
    host_name = row.get("HostName") or row.get("IP") or "node"
    server = profile.get("server")
    proxy: dict[str, object] = {
        "name": f"VPNGate-{_slug(country)}-{_slug(host_name)}-{_slug(server)}",
        "type": "openvpn",
        "server": server,
        "port": profile["port"],
        "proto": profile["proto"],
        "udp": profile["udp"],
        "ca": ca,
    }
    if cert:
        proxy["cert"] = cert
        proxy["key"] = key
    else:
        proxy["username"] = str(username).strip()
        proxy["password"] = "" if password is None else str(password)
    for field in (
        "tls-auth",
        "key-direction",
        "tls-crypt",
        "tls-crypt-v2",
        "ping",
        "ping-restart",
        "tran-window",
        "handshake-timeout",
        "dev",
    ):
        if profile.get(field) not in (None, "", []):
            proxy[field] = key_direction if field == "key-direction" else profile[field]
    if profile.get("cipher") not in (None, ""):
        proxy["cipher"] = _normalize_cipher(str(profile["cipher"]))
    if profile.get("data-ciphers") not in (None, "", []):
        values = profile["data-ciphers"]
        if not isinstance(values, list):
            values = _split_cipher_list(str(values))
        proxy["data-ciphers"] = [_normalize_cipher(str(value)) for value in values]
    if profile.get("data-ciphers-fallback") not in (None, ""):
        proxy["data-ciphers-fallback"] = _normalize_cipher(
            str(profile["data-ciphers-fallback"])
        )
    if profile.get("auth") not in (None, ""):
        proxy["auth"] = _normalize_auth(str(profile["auth"]))
    if profile.get("comp-lzo") not in (None, ""):
        comp_lzo = str(profile["comp-lzo"]).lower()
        if comp_lzo not in {"yes", "no", "adaptive"}:
            raise ValueError(f"unsupported Mihomo OpenVPN comp-lzo: {comp_lzo}")
        proxy["comp-lzo"] = comp_lzo
    return validate_openvpn_entry(proxy)


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def render_mihomo_yaml(proxies: list[dict[str, object]]) -> str:
    """Render the fixed Mihomo proxy schema without a YAML dependency."""
    block_fields = {"ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2"}
    lines = ["proxies:"]
    for proxy in proxies:
        first = True
        for key, value in proxy.items():
            prefix = "  - " if first else "    "
            first = False
            if key in block_fields:
                lines.append(f"{prefix}{key}: |-")
                content = str(value).splitlines() or [""]
                lines.extend(f"      {line}" for line in content)
            elif key == "type" and value == "openvpn":
                lines.append(f"{prefix}{key}: openvpn")
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_outputs(
    output_dir: Path,
    source: str,
    snapshot: str,
    valid_rows: list[dict[str, object]],
    failures: list[dict[str, str]],
    proxies: list[dict[str, object]],
    source_row_count: int | None = None,
) -> dict[str, object]:
    """Write the raw snapshot, audit metadata, and Mihomo YAML atomically."""
    generated_at = dt.datetime.now(dt.timezone.utc)
    snapshot_bytes = snapshot.encode("utf-8")
    metadata: dict[str, object] = {
        "schema_version": 1,
        "source": source,
        "generated_at": generated_at.timestamp(),
        "generated_at_iso": generated_at.isoformat(),
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "source_row_count": source_row_count
        if source_row_count is not None
        else len(valid_rows) + len(failures),
        "valid_openvpn_count": len(valid_rows),
        "exported_proxy_count": len(proxies),
        "failure_count": len(failures),
        "failures": failures,
    }
    _atomic_write(output_dir / "vpngate.csv", snapshot_bytes)
    _atomic_write(
        output_dir / "vpngate.meta.json",
        (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    output_files = ["vpngate.csv", "vpngate.meta.json"]
    if proxies:
        _atomic_write(
            output_dir / "mihomo-openvpn.yaml",
            render_mihomo_yaml(proxies).encode("utf-8"),
        )
        output_files.append("mihomo-openvpn.yaml")
    return {**metadata, "output_files": output_files}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download VPN Gate OpenVPN nodes and generate Mihomo YAML."
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", default="./vpngate-output")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--username", help="username for auth-user-pass profiles")
    parser.add_argument("--password", help="password for auth-user-pass profiles")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.username is None) != (args.password is None):
        print("--username and --password must be provided together", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2
    try:
        snapshot = fetch_snapshot(args.source, args.timeout)
        rows, parse_failures = parse_snapshot(snapshot)
        failures = list(parse_failures)
        proxies: list[dict[str, object]] = []
        for row in rows:
            try:
                proxies.append(build_mihomo_proxy(row, args.username, args.password))
            except ValueError as exc:
                failures.append(_row_failure(int(row["row_number"]), row, str(exc)))
        summary = write_outputs(
            Path(args.output_dir),
            args.source,
            snapshot,
            rows,
            failures,
            proxies,
            source_row_count=len(rows) + len(parse_failures),
        )
        print(
            json.dumps(
                {
                    "source": args.source,
                    "output_dir": str(Path(args.output_dir)),
                    "valid_openvpn_count": summary["valid_openvpn_count"],
                    "exported_proxy_count": summary["exported_proxy_count"],
                    "failure_count": summary["failure_count"],
                    "output_files": summary["output_files"],
                },
                ensure_ascii=False,
            )
        )
        if not proxies:
            print("No convertible OpenVPN proxies were found", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"VPN Gate export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
