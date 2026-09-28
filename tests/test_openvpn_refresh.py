from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, select

from core.db import OpenVPNProxyModel, engine
from domain.openvpn_proxies import OpenVPNProbeResult, OpenVPNProxyRuntimeRecord, openvpn_proxy_identity
from infrastructure.openvpn_proxies_repository import OpenVPNProxiesRepository


CA = "-----BEGIN CERTIFICATE-----\nQ0E=\n-----END CERTIFICATE-----"
CERT = "-----BEGIN CERTIFICATE-----\nQ0V=\n-----END CERTIFICATE-----"
KEY = "-----BEGIN PRIVATE KEY-----\nS0U=\n-----END PRIVATE KEY-----"


def entry(name: str, server: str) -> dict[str, object]:
    return {
        "name": name,
        "type": "openvpn",
        "server": server,
        "port": 443,
        "proto": "tcp",
        "ca": CA,
        "cert": CERT,
        "key": KEY,
    }


def runtime_record(proxy_id: int, item: dict[str, object]) -> OpenVPNProxyRuntimeRecord:
    return OpenVPNProxyRuntimeRecord(
        id=proxy_id,
        name=str(item["name"]),
        server=str(item["server"]),
        port=int(item["port"]),
        proto=str(item["proto"]),
        config=item,
    )


class FakeManager:
    instances: list["FakeManager"] = []
    healthy_by_server: dict[str, bool] = {}

    def __init__(self, profiles, **kwargs):
        self.profiles = list(profiles)
        self.kwargs = kwargs
        self.started = False
        self.released = False
        type(self).instances.append(self)

    def start_task(self):
        self.started = True
        return "http://127.0.0.1:38123"

    def probe_all(self):
        return [
            OpenVPNProbeResult(
                profile=profile,
                healthy=self.healthy_by_server.get(profile.server, True),
                reason="proxy-health-failed"
                if not self.healthy_by_server.get(profile.server, True)
                else "",
            )
            for profile in self.profiles
        ]

    def release(self):
        self.released = True


def build_service(monkeypatch, *, direct_results=(True, True), entries=None):
    from application.openvpn_refresh import OpenVPNRefreshService

    entries = entries or [entry("node-1", "198.51.100.1")]
    rows = [
        {
            "row_number": index + 2,
            "HostName": str(item["name"]),
            "IP": str(item["server"]),
            "profile": {"server": item["server"], "port": item["port"], "proto": item["proto"]},
        }
        for index, item in enumerate(entries)
    ]
    direct = iter(direct_results)
    FakeManager.instances = []
    FakeManager.healthy_by_server = {}
    return OpenVPNRefreshService(
        repository=OpenVPNProxiesRepository(),
        manager_factory=FakeManager,
        snapshot_fetcher=lambda _source, _timeout: "snapshot",
        snapshot_parser=lambda _snapshot: (rows, []),
        proxy_builder=lambda row: entries[int(row["row_number"]) - 2],
        direct_probe=lambda _url: next(direct),
        source="https://www.vpngate.net/api/iphone/",
    )


def add_model(
    item: dict[str, object],
    *,
    source: str,
    refresh_healthy: bool | None,
    is_active: bool = True,
    success_count: int = 0,
    fail_count: int = 0,
    last_checked: datetime | None = None,
):
    model = OpenVPNProxyModel(
        name=str(item["name"]),
        server=str(item["server"]),
        port=int(item["port"]),
        identity=openvpn_proxy_identity(str(item["name"]), str(item["server"]), int(item["port"])),
        proto=str(item["proto"]),
        mihomo_config_json=json.dumps(item),
        source=source,
        refresh_healthy=refresh_healthy,
        is_active=is_active,
        success_count=success_count,
        fail_count=fail_count,
        last_checked=last_checked,
    )
    with Session(engine) as session:
        session.add(model)
        session.commit()
        session.refresh(model)
    return model.id


def get_model(proxy_id: int) -> OpenVPNProxyModel:
    with Session(engine) as session:
        return session.get(OpenVPNProxyModel, proxy_id)


def test_refresh_invalid_timezone_is_audited_and_does_not_raise(monkeypatch):
    from application.openvpn_refresh import OpenVPNRefreshService

    monkeypatch.setenv("VPN_GATE_REFRESH_TIMEZONE", "Not/AZone")
    service = OpenVPNRefreshService(repository=OpenVPNProxiesRepository())

    result = service.refresh_once()

    assert result["status"] == "failed"
    assert result["reason"] == "invalid-timezone"
    runs = OpenVPNProxiesRepository().list_refresh_runs()
    assert runs[0].status == "failed"


def test_refresh_success_preserves_manual_controls_and_runtime_statistics(monkeypatch):
    old_checked = datetime(2026, 9, 28)
    old_vpngate = entry("old", "198.51.100.2")
    current = entry("node-1", "198.51.100.1")
    manual = entry("manual", "198.51.100.3")
    old_id = add_model(
        old_vpngate,
        source="vpngate",
        refresh_healthy=True,
        is_active=False,
        success_count=3,
        fail_count=4,
        last_checked=old_checked,
    )
    current_id = add_model(
        current,
        source="vpngate",
        refresh_healthy=False,
        is_active=False,
        success_count=5,
        fail_count=6,
        last_checked=old_checked,
    )
    manual_id = add_model(manual, source="manual", refresh_healthy=None)

    service = build_service(monkeypatch, entries=[current])
    result = service.refresh_once(scheduled_for="2026-09-28T00:00:00+08:00")

    assert result["status"] == "success"
    assert get_model(old_id).refresh_healthy is False
    refreshed = get_model(current_id)
    assert refreshed.refresh_healthy is True
    assert refreshed.is_active is False
    assert refreshed.success_count == 5
    assert refreshed.fail_count == 6
    assert refreshed.last_checked == old_checked
    untouched = get_model(manual_id)
    assert untouched.source == "manual"
    assert {item.name for item in OpenVPNProxiesRepository().active_runtime_records()} == {"manual"}


def test_refresh_manual_identity_collision_does_not_overwrite_manual_row(monkeypatch):
    current = entry("node-1", "198.51.100.1")
    manual_id = add_model(current, source="manual", refresh_healthy=None)

    service = build_service(monkeypatch, entries=[current])
    result = service.refresh_once()

    assert result["status"] == "success"
    assert any(item["reason"] == "manual_identity_collision" for item in result["failures"])
    manual = get_model(manual_id)
    assert manual.source == "manual"
    assert manual.refresh_healthy is None


def test_refresh_direct_probe_failure_preserves_previous_snapshot(monkeypatch):
    current = entry("node-1", "198.51.100.1")
    proxy_id = add_model(current, source="vpngate", refresh_healthy=True)

    service = build_service(monkeypatch, direct_results=(False, True), entries=[current])
    result = service.refresh_once()

    assert result["status"] == "failed"
    assert result["reason"] == "direct-probe-failed"
    assert get_model(proxy_id).refresh_healthy is True
    assert FakeManager.instances == []


def test_refresh_post_probe_failure_preserves_previous_snapshot(monkeypatch):
    current = entry("node-1", "198.51.100.1")
    proxy_id = add_model(current, source="vpngate", refresh_healthy=True)

    service = build_service(monkeypatch, direct_results=(True, False), entries=[current])
    result = service.refresh_once()

    assert result["status"] == "failed"
    assert result["reason"] == "direct-probe-failed"
    assert get_model(proxy_id).refresh_healthy is True
    assert FakeManager.instances[0].released is True


def test_refresh_zero_healthy_is_degraded_and_does_not_replace_snapshot(monkeypatch):
    current = entry("node-1", "198.51.100.1")
    proxy_id = add_model(current, source="vpngate", refresh_healthy=True)
    service = build_service(monkeypatch, entries=[current])
    FakeManager.healthy_by_server = {"198.51.100.1": False}
    result = service.refresh_once()

    assert result["status"] == "degraded"
    assert get_model(proxy_id).refresh_healthy is True


def test_refresh_probe_uses_unlimited_manager_without_reporting(monkeypatch):
    entries = [entry(f"node-{index}", f"198.51.100.{index}") for index in range(1, 4)]
    service = build_service(monkeypatch, entries=entries)

    result = service.refresh_once()

    assert result["status"] == "success"
    manager = FakeManager.instances[0]
    assert manager.kwargs["max_candidates"] == 0
    assert manager.kwargs["report_callback"] is None
    assert [profile.server for profile in manager.profiles] == [
        "198.51.100.1",
        "198.51.100.2",
        "198.51.100.3",
    ]


def test_refresh_deduplicates_identity_before_probe(monkeypatch):
    first = entry("node-1", "198.51.100.1")
    duplicate = {**first, "proto": "udp"}
    service = build_service(monkeypatch, entries=[first, duplicate])

    result = service.refresh_once()

    assert result["status"] == "success"
    assert len(FakeManager.instances[0].profiles) == 1
    assert any(item["reason"] == "duplicate_identity" for item in result["failures"])


def test_manual_refresh_endpoint_requires_authentication(client, monkeypatch):
    class Service:
        def refresh_once(self):
            return {"status": "success"}

    monkeypatch.setenv("APP_PASSWORD", "secret")
    monkeypatch.setattr("api.proxies.openvpn_refresh_service", Service())

    response = client.post("/api/proxies/openvpn/refresh")
    authorized = client.post(
        "/api/proxies/openvpn/refresh",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 401
    assert authorized.status_code == 200


def test_manual_refresh_endpoint_runs_refresh_service(client, monkeypatch):
    class Service:
        def refresh_once(self):
            return {"status": "success", "healthy_count": 1}

    monkeypatch.setattr("api.proxies.openvpn_refresh_service", Service())

    response = client.post("/api/proxies/openvpn/refresh")

    assert response.status_code == 200
    assert response.json() == {"status": "success", "healthy_count": 1}


def test_refresh_runs_endpoint_returns_audited_rows(client, monkeypatch):
    class Service:
        def list_runs(self, _limit):
            return [{"id": 1, "status": "success", "failures": []}]

    monkeypatch.setattr("api.proxies.openvpn_refresh_service", Service())

    response = client.get("/api/proxies/openvpn/refresh/runs")

    assert response.status_code == 200
    assert response.json() == [{"id": 1, "status": "success", "failures": []}]


def test_manual_refresh_endpoint_returns_conflict_when_busy(client, monkeypatch):
    from application.openvpn_refresh import RefreshBusyError

    class Service:
        def refresh_once(self):
            raise RefreshBusyError("already running")

    monkeypatch.setattr("api.proxies.openvpn_refresh_service", Service())

    response = client.post("/api/proxies/openvpn/refresh")

    assert response.status_code == 409
    assert response.json()["detail"] == "already running"
