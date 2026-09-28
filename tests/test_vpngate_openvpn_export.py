from __future__ import annotations

import base64
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import vpngate_openvpn_export as exporter


CA_PEM = """-----BEGIN CERTIFICATE-----
Q0EtQ09OVEVOVA==
-----END CERTIFICATE-----"""
CERT_PEM = """-----BEGIN CERTIFICATE-----
Q0VSVF9DT05URU5U
-----END CERTIFICATE-----"""
KEY_PEM = """-----BEGIN PRIVATE KEY-----
S0VZX0NPTlRFTlQ=
-----END PRIVATE KEY-----"""

VALID_PROFILE = f"""client
dev tun
proto tcp
remote 198.51.100.10 443 tcp
cipher AES-128-CBC
data-ciphers AES-128-CBC:AES-256-GCM
auth SHA1
<ca>
{CA_PEM}
</ca>
<cert>
{CERT_PEM}
</cert>
<key>
{KEY_PEM}
</key>
"""


def encoded_profile(profile: str = VALID_PROFILE) -> str:
    return base64.b64encode(profile.encode()).decode()


def snapshot_text() -> str:
    fields = [
        "HostName",
        "IP",
        "Score",
        "Ping",
        "Speed",
        "CountryLong",
        "CountryShort",
        "NumVpnSessions",
        "OpenVPN_ConfigData_Base64",
    ]
    rows = [
        [
            "vpn-one",
            "198.51.100.10",
            "10",
            "20",
            "300",
            "Japan",
            "JP",
            "2",
            encoded_profile(),
        ],
        [
            "broken",
            "198.51.100.11",
            "",
            "",
            "",
            "Japan",
            "JP",
            "0",
            "not-base64",
        ],
    ]
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([f"#{fields[0]}", *fields[1:]])
    writer.writerows(rows)
    return output.getvalue()


def test_parse_snapshot_decodes_valid_rows_and_reports_invalid_rows():
    rows, failures = exporter.parse_snapshot(snapshot_text())

    assert len(rows) == 1
    assert rows[0]["HostName"] == "vpn-one"
    assert rows[0]["profile"]["server"] == "198.51.100.10"
    assert rows[0]["profile"]["port"] == 443
    assert failures[0]["host_name"] == "broken"
    assert "base64" in failures[0]["reason"].lower()


def test_parse_openvpn_profile_maps_transport_and_inline_blocks():
    profile = exporter.parse_openvpn_profile(VALID_PROFILE)

    assert profile["server"] == "198.51.100.10"
    assert profile["port"] == 443
    assert profile["proto"] == "tcp"
    assert profile["udp"] is False
    assert profile["ca"] == CA_PEM
    assert profile["cert"] == CERT_PEM
    assert profile["key"] == KEY_PEM
    assert profile["data-ciphers"] == ["AES-128-CBC", "AES-256-GCM"]


def test_parse_openvpn_profile_rejects_external_tls_auth_reference():
    with pytest.raises(ValueError, match="external tls-auth"):
        exporter.parse_openvpn_profile(VALID_PROFILE + "tls-auth ta.key 1\n")


def test_build_mihomo_proxy_uses_stable_metadata_name():
    rows, _ = exporter.parse_snapshot(snapshot_text())

    proxy = exporter.build_mihomo_proxy(rows[0])

    assert proxy["name"] == "VPNGate-JP-vpn-one-198.51.100.10"
    assert proxy["type"] == "openvpn"
    assert proxy["server"] == "198.51.100.10"
    assert proxy["port"] == 443
    assert proxy["udp"] is False


def test_parse_snapshot_deduplicates_ip_and_reports_duplicate():
    source = snapshot_text()
    duplicate = source.rstrip("\n") + "\n" + source.splitlines()[1] + "\n"

    rows, failures = exporter.parse_snapshot(duplicate)

    assert len(rows) == 1
    assert any(failure["reason"] == "duplicate IP" for failure in failures)


def test_render_mihomo_yaml_contains_openvpn_proxy_and_multiline_values():
    rows, _ = exporter.parse_snapshot(snapshot_text())

    rendered = exporter.render_mihomo_yaml([exporter.build_mihomo_proxy(rows[0])])

    assert rendered.startswith("proxies:\n")
    assert "type: openvpn" in rendered
    assert "udp: false" in rendered
    assert "ca: |-" in rendered
    assert "      Q0EtQ09OVEVOVA==" in rendered
    assert "cert: |-" in rendered
    assert "key: |-" in rendered


def test_write_outputs_preserves_snapshot_and_records_metadata(tmp_path: Path):
    snapshot = snapshot_text()
    rows, failures = exporter.parse_snapshot(snapshot)
    proxies = [exporter.build_mihomo_proxy(rows[0])]

    summary = exporter.write_outputs(
        tmp_path,
        "https://example.test/vpngate.csv",
        snapshot,
        rows,
        failures,
        proxies,
    )

    assert (tmp_path / "vpngate.csv").read_text() == snapshot
    metadata = json.loads((tmp_path / "vpngate.meta.json").read_text())
    assert metadata["schema_version"] == 1
    assert metadata["source_row_count"] == 2
    assert metadata["valid_openvpn_count"] == 1
    assert metadata["exported_proxy_count"] == 1
    assert metadata["failure_count"] == 1
    assert summary["output_files"] == [
        "vpngate.csv",
        "vpngate.meta.json",
        "mihomo-openvpn.yaml",
    ]


def test_cli_help_works_when_invoked_by_script_path():
    script = Path(__file__).parents[1] / "tools" / "vpngate_openvpn_export.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--output-dir" in result.stdout


def test_main_runs_export_with_monkeypatched_source(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(exporter, "fetch_snapshot", lambda source, timeout: snapshot_text())

    exit_code = exporter.main(
        ["--source", "https://example.test/feed", "--output-dir", str(tmp_path)]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["exported_proxy_count"] == 1
    assert (tmp_path / "mihomo-openvpn.yaml").exists()


def test_main_rejects_partial_auth_credentials(monkeypatch):
    def fail_if_called(*args, **kwargs):
        pytest.fail("network fetch should not happen")

    monkeypatch.setattr(exporter, "fetch_snapshot", fail_if_called)

    assert exporter.main(["--username", "user"]) != 0


def test_main_reports_fetch_failure(tmp_path: Path, monkeypatch, capsys):
    def fail_fetch(source, timeout):
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr(exporter, "fetch_snapshot", fail_fetch)

    exit_code = exporter.main(["--output-dir", str(tmp_path)])

    assert exit_code != 0
    assert "feed unavailable" in capsys.readouterr().err
    assert not (tmp_path / "mihomo-openvpn.yaml").exists()


def test_build_mihomo_proxy_rejects_values_mihomo_cannot_parse():
    rows, _ = exporter.parse_snapshot(snapshot_text())
    row = rows[0]

    row["profile"]["cipher"] = "BF-CBC"
    with pytest.raises(ValueError, match="cipher"):
        exporter.build_mihomo_proxy(row)

    row["profile"]["cipher"] = "AES-128-CBC"
    row["profile"]["ca"] = "not a PEM block"
    with pytest.raises(ValueError, match="ca.*PEM"):
        exporter.build_mihomo_proxy(row)

    row["profile"]["ca"] = CA_PEM
    row["profile"].pop("cert")
    row["profile"].pop("key")
    with pytest.raises(ValueError, match="username"):
        exporter.build_mihomo_proxy(row, username="", password="secret")


def test_main_does_not_overwrite_previous_yaml_when_no_proxy_is_convertible(
    tmp_path: Path, monkeypatch, capsys
):
    row = {
        "row_number": 2,
        "HostName": "auth-only",
        "IP": "198.51.100.12",
        "CountryShort": "JP",
        "profile": {
            "server": "198.51.100.12",
            "port": 443,
            "proto": "tcp",
            "udp": False,
            "ca": CA_PEM,
        },
    }
    old_yaml = "proxies:\n  - name: old-good-output\n"
    (tmp_path / "mihomo-openvpn.yaml").write_text(old_yaml)
    monkeypatch.setattr(exporter, "fetch_snapshot", lambda source, timeout: "raw")
    monkeypatch.setattr(exporter, "parse_snapshot", lambda snapshot: ([row], []))

    exit_code = exporter.main(["--output-dir", str(tmp_path)])

    assert exit_code == 1
    assert (tmp_path / "mihomo-openvpn.yaml").read_text() == old_yaml
    metadata = json.loads((tmp_path / "vpngate.meta.json").read_text())
    assert metadata["source_row_count"] == 1
    assert metadata["exported_proxy_count"] == 0
    assert metadata["failure_count"] == 1
    assert "mihomo-openvpn.yaml" not in json.loads(capsys.readouterr().out)["output_files"]
