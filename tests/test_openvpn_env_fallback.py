"""可选功能的环境变量写坏，不能让整个应用起不来。

环境变量里的坏值：打一行警告，回落到默认值。
构造参数里的坏值：照旧抛 ValueError（那是调用方写错了代码）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.openvpn_refresh import OpenVPNRefreshService
from application.openvpn_refresh_scheduler import DEFAULT_MAX_ATTEMPTS, OpenVPNRefreshScheduler

REPO_ROOT = Path(__file__).resolve().parents[1]

BAD_ENV = {
    "VPN_GATE_FETCH_TIMEOUT": "abc",
    "VPN_GATE_HEALTHCHECK_TIMEOUT": "abc",
    "VPN_GATE_MAX_SNAPSHOT_BYTES": "abc",
    "VPN_GATE_REFRESH_RETRIES": "abc",
}


def _service() -> OpenVPNRefreshService:
    return OpenVPNRefreshService(repository=object())


def _scheduler() -> OpenVPNRefreshScheduler:
    return OpenVPNRefreshScheduler(service=SimpleNamespace(repository=object()))


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_bad_fetch_timeout_env_falls_back_to_default(monkeypatch, capsys, raw):
    monkeypatch.setenv("VPN_GATE_FETCH_TIMEOUT", raw)
    assert _service().fetch_timeout == 30
    assert "VPN_GATE_FETCH_TIMEOUT" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["abc", "0", "-1.5"])
def test_bad_healthcheck_timeout_env_falls_back_to_default(monkeypatch, capsys, raw):
    monkeypatch.setenv("VPN_GATE_HEALTHCHECK_TIMEOUT", raw)
    assert _service().health_timeout == 20.0
    assert "VPN_GATE_HEALTHCHECK_TIMEOUT" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["abc", "0", "-1"])
def test_bad_max_snapshot_bytes_env_falls_back_to_default(monkeypatch, capsys, raw):
    monkeypatch.setenv("VPN_GATE_MAX_SNAPSHOT_BYTES", raw)
    assert _service().max_snapshot_bytes == 12582912
    assert "VPN_GATE_MAX_SNAPSHOT_BYTES" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["abc", "0", "-1"])
def test_bad_refresh_retries_env_falls_back_to_default(monkeypatch, capsys, raw):
    monkeypatch.setenv("VPN_GATE_REFRESH_RETRIES", raw)
    assert _scheduler().max_attempts == DEFAULT_MAX_ATTEMPTS
    assert "VPN_GATE_REFRESH_RETRIES" in capsys.readouterr().out


def test_valid_env_values_are_still_used(monkeypatch, capsys):
    monkeypatch.setenv("VPN_GATE_FETCH_TIMEOUT", "45")
    monkeypatch.setenv("VPN_GATE_HEALTHCHECK_TIMEOUT", "7.5")
    monkeypatch.setenv("VPN_GATE_MAX_SNAPSHOT_BYTES", "1024")
    monkeypatch.setenv("VPN_GATE_REFRESH_RETRIES", "5")
    service = _service()
    assert service.fetch_timeout == 45
    assert service.health_timeout == 7.5
    assert service.max_snapshot_bytes == 1024
    assert _scheduler().max_attempts == 5
    assert capsys.readouterr().out == ""


def test_explicit_bad_arguments_still_raise():
    with pytest.raises(ValueError, match="VPN_GATE_MAX_SNAPSHOT_BYTES"):
        OpenVPNRefreshService(repository=object(), max_snapshot_bytes=-1)
    with pytest.raises(ValueError, match="VPN_GATE_REFRESH_RETRIES"):
        OpenVPNRefreshScheduler(service=SimpleNamespace(repository=object()), max_attempts=0)


def test_app_imports_with_all_bad_env_values():
    env = {**os.environ, **BAD_ENV}
    proc = subprocess.run(
        [sys.executable, "-c", "import main; from application.openvpn_refresh_scheduler import openvpn_refresh_scheduler"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
