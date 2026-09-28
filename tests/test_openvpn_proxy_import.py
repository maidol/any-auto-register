from __future__ import annotations

import pytest
import yaml
from sqlalchemy import inspect
from sqlmodel import Session, create_engine, select

from core import db
from core.db import OpenVPNProxyModel, engine
from core.openvpn_config import parse_mihomo_openvpn_yaml
from infrastructure.openvpn_proxies_repository import OpenVPNProxiesRepository


VALID_ENTRY = {
    "name": "VPNGate-JP-test-198.51.100.10",
    "type": "openvpn",
    "server": "198.51.100.10",
    "port": 443,
    "proto": "tcp",
    "ca": "-----BEGIN CERTIFICATE-----\nQ0E=\n-----END CERTIFICATE-----",
    "cert": "-----BEGIN CERTIFICATE-----\nQ0V=\n-----END CERTIFICATE-----",
    "key": "-----BEGIN PRIVATE KEY-----\nS0U=\n-----END PRIVATE KEY-----",
}


def test_import_parser_accepts_valid_openvpn_entry():
    result = parse_mihomo_openvpn_yaml({"proxies": [VALID_ENTRY]})

    assert result.entries[0]["name"] == VALID_ENTRY["name"]
    assert result.entries[0]["server"] == "198.51.100.10"


def test_import_parser_normalizes_malformed_yaml_error():
    with pytest.raises(ValueError, match="Malformed Mihomo YAML"):
        parse_mihomo_openvpn_yaml("proxies:\n  - [unterminated")


def test_import_parser_rejects_empty_password_without_cert_key():
    passwordless = {
        key: value for key, value in VALID_ENTRY.items() if key not in {"cert", "key"}
    }
    passwordless.update(username="vpn-user", password="")

    result = parse_mihomo_openvpn_yaml({"proxies": [passwordless]})

    assert result.entries == []
    assert "username and password" in result.failures[0]["reason"]


def test_import_parser_rejects_non_openvpn_and_invalid_pem():
    result = parse_mihomo_openvpn_yaml(
        {"proxies": [{"name": "http", "type": "http", "server": "x", "port": 80}]}
    )
    assert result.entries == []
    assert result.failures[0]["reason"] == "unsupported proxy type: http"

    bad = {"proxies": [{**VALID_ENTRY, "ca": "not pem"}]}
    result = parse_mihomo_openvpn_yaml(bad)
    assert result.entries == []
    assert "ca" in result.failures[0]["reason"]


def test_import_parser_rejects_unbound_runtime_references():
    bad = {**VALID_ENTRY, "dialer-proxy": "missing-group"}

    result = parse_mihomo_openvpn_yaml({"proxies": [bad]})

    assert result.entries == []
    assert "dialer-proxy" in result.failures[0]["reason"]


def test_import_parser_rejects_invalid_tls_auth_static_key():
    bad = {**VALID_ENTRY, "tls-auth": "not a static key"}

    result = parse_mihomo_openvpn_yaml({"proxies": [bad]})

    assert result.entries == []
    assert "tls-auth" in result.failures[0]["reason"]


def test_import_parser_deduplicates_name_server_port():
    result = parse_mihomo_openvpn_yaml({"proxies": [VALID_ENTRY, VALID_ENTRY]})

    assert len(result.entries) == 1
    assert result.failures[0]["reason"] == "duplicate OpenVPN name"


def test_import_parser_rejects_duplicate_mihomo_names():
    duplicate_name = {**VALID_ENTRY, "server": "198.51.100.11"}

    result = parse_mihomo_openvpn_yaml({"proxies": [VALID_ENTRY, duplicate_name]})

    assert len(result.entries) == 1
    assert result.failures[0]["reason"] == "duplicate OpenVPN name"


def test_import_is_idempotent_and_keeps_private_config_out_of_metadata():
    repo = OpenVPNProxiesRepository()

    first = repo.import_entries([VALID_ENTRY], region="JP")
    second = repo.import_entries([VALID_ENTRY], region="JP")

    assert first.added == 1
    assert second.added == 0
    assert second.existing == 1
    assert len(repo.list_metadata()) == 1
    item = repo.list_metadata()[0]
    assert item.name == VALID_ENTRY["name"]
    assert not hasattr(item, "ca")
    assert not hasattr(item, "key")
    with Session(engine) as session:
        stored = session.get(OpenVPNProxyModel, item.id)
        assert stored.identity
        assert "PRIVATE KEY" in stored.mihomo_config_json


def test_reimport_updates_region_for_same_identity():
    repo = OpenVPNProxiesRepository()

    repo.import_entries([VALID_ENTRY], region="JP")
    result = repo.import_entries([VALID_ENTRY], region="US")

    assert result.updated == 1
    assert repo.list_metadata(region="JP") == []
    assert repo.list_metadata(region="US")[0].name == VALID_ENTRY["name"]


def test_same_name_with_different_server_and_port_coexists():
    repo = OpenVPNProxiesRepository()
    changed = {**VALID_ENTRY, "server": "198.51.100.11", "port": 1194}

    first = repo.import_entries([VALID_ENTRY], region="JP")
    second = repo.import_entries([changed], region="US")
    active = repo.active_runtime_records()

    assert first.added == 1
    assert second.added == 1
    assert len(active) == 2
    assert {(item.server, item.port) for item in active} == {
        ("198.51.100.10", 443),
        ("198.51.100.11", 1194),
    }


def test_legacy_duplicate_identities_converge_when_init_db_runs_twice(monkeypatch, tmp_path):
    migration_engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-openvpn.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", migration_engine)
    with migration_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE openvpn_proxies (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                server VARCHAR NOT NULL,
                port INTEGER NOT NULL,
                proto VARCHAR NOT NULL,
                mihomo_config_json VARCHAR NOT NULL,
                region VARCHAR NOT NULL,
                success_count INTEGER NOT NULL,
                fail_count INTEGER NOT NULL,
                is_active BOOLEAN NOT NULL,
                last_checked DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO openvpn_proxies VALUES
            (1, 'VPN', 'VPN.example', 1194, 'udp', '{"old":true}', 'JP', 2, 3, 1, NULL),
            (2, ' vpn ', 'vpn.EXAMPLE', 1194, 'tcp', '{"new":true}', 'US', 4, 5, 0, NULL)
            """
        )

    monkeypatch.setattr(db, "_migrate_legacy_provider_keys", lambda: None)
    monkeypatch.setattr(db, "_cleanup_non_real_providers", lambda: None)
    monkeypatch.setattr(db, "_cleanup_empty_provider_settings", lambda: None)
    monkeypatch.setattr("core.account_graph.sync_all_account_graphs", lambda session: None)
    monkeypatch.setattr(
        "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository.ensure_seeded",
        lambda self: None,
    )

    db.init_db()
    db.init_db()

    with Session(migration_engine) as session:
        items = session.exec(select(OpenVPNProxyModel)).all()
    assert len(items) == 1
    assert items[0].id == 2
    assert items[0].name == " vpn "
    assert items[0].mihomo_config_json == '{"new":true}'
    assert items[0].region == "US"
    assert items[0].success_count == 6
    assert items[0].fail_count == 8
    assert items[0].is_active is False
    assert items[0].identity
    indexes = inspect(migration_engine).get_indexes("openvpn_proxies")
    assert any(index["name"] == "uq_openvpn_proxies_identity" and index["unique"] for index in indexes)


def test_import_mihomo_endpoint_returns_report(client):
    response = client.post(
        "/api/proxies/import/mihomo",
        json={"content": yaml.safe_dump({"proxies": [VALID_ENTRY]}), "region": "JP"},
    )

    assert response.status_code == 200
    assert response.json()["added"] == 1


def test_import_mihomo_endpoint_rejects_malformed_yaml(client):
    response = client.post(
        "/api/proxies/import/mihomo",
        json={"content": "proxies:\n  - [unterminated"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Malformed Mihomo YAML"


def test_openvpn_list_does_not_return_private_material(client):
    client.post(
        "/api/proxies/import/mihomo",
        json={"content": yaml.safe_dump({"proxies": [VALID_ENTRY]})},
    )

    items = client.get("/api/proxies/openvpn").json()

    assert items[0]["name"] == VALID_ENTRY["name"]
    assert "key" not in items[0]
    assert "mihomo_config_json" not in items[0]
