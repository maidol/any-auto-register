from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from core.db import OpenVPNProxyModel, engine
from domain.openvpn_proxies import (
    OpenVPNImportSummary,
    OpenVPNProxyRecord,
    OpenVPNProxyRuntimeRecord,
    openvpn_proxy_identity,
)


def _to_metadata(model: OpenVPNProxyModel) -> OpenVPNProxyRecord:
    return OpenVPNProxyRecord(
        id=int(model.id or 0),
        name=model.name,
        server=model.server,
        port=model.port,
        proto=model.proto,
        region=model.region,
        success_count=model.success_count,
        fail_count=model.fail_count,
        is_active=bool(model.is_active),
        last_checked=model.last_checked,
    )


def _to_runtime(model: OpenVPNProxyModel) -> OpenVPNProxyRuntimeRecord:
    record = _to_metadata(model)
    return OpenVPNProxyRuntimeRecord(
        **asdict(record),
        config=json.loads(model.mihomo_config_json),
    )


class OpenVPNProxiesRepository:
    def import_entries(self, entries: list[dict[str, object]], region: str = "") -> OpenVPNImportSummary:
        summary = OpenVPNImportSummary(total=len(entries), failures=[])
        with Session(engine) as session:
            for entry in entries:
                name = str(entry["name"])
                server = str(entry["server"])
                port = int(entry["port"])
                proto = str(entry.get("proto") or "udp")
                identity = openvpn_proxy_identity(name, server, port)
                config_json = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                existing = session.exec(
                    select(OpenVPNProxyModel)
                    .where(OpenVPNProxyModel.identity == identity)
                ).first()
                if existing:
                    unchanged = (
                        existing.name == name
                        and existing.server == server
                        and existing.port == port
                        and existing.proto == proto
                        and existing.mihomo_config_json == config_json
                        and existing.region == region
                    )
                    if unchanged:
                        summary.existing += 1
                        continue
                    existing.name = name
                    existing.server = server
                    existing.port = port
                    existing.proto = proto
                    existing.mihomo_config_json = config_json
                    existing.region = region
                    session.add(existing)
                    summary.updated += 1
                    continue
                insert_stmt = sqlite_insert(OpenVPNProxyModel).values(
                    name=name,
                    server=server,
                    port=port,
                    identity=identity,
                    proto=proto,
                    mihomo_config_json=config_json,
                    region=region,
                )
                session.execute(
                    insert_stmt.on_conflict_do_update(
                        index_elements=[OpenVPNProxyModel.identity],
                        set_={
                            "name": insert_stmt.excluded.name,
                            "server": insert_stmt.excluded.server,
                            "port": insert_stmt.excluded.port,
                            "proto": insert_stmt.excluded.proto,
                            "mihomo_config_json": insert_stmt.excluded.mihomo_config_json,
                            "region": insert_stmt.excluded.region,
                        },
                    )
                )
                summary.added += 1
            session.commit()
        return summary

    def list_metadata(self, region: str = "") -> list[OpenVPNProxyRecord]:
        with Session(engine) as session:
            statement = select(OpenVPNProxyModel)
            if region:
                statement = statement.where(OpenVPNProxyModel.region == region)
            items = session.exec(statement).all()
        return [_to_metadata(item) for item in items]

    def active_runtime_records(self, region: str = "") -> list[OpenVPNProxyRuntimeRecord]:
        with Session(engine) as session:
            statement = (
                select(OpenVPNProxyModel)
                .where(OpenVPNProxyModel.is_active == True)
                .order_by(OpenVPNProxyModel.id.desc())
            )
            if region:
                statement = statement.where(OpenVPNProxyModel.region == region)
            items = session.exec(statement).all()
        records: list[OpenVPNProxyRuntimeRecord] = []
        seen_identities: set[str] = set()
        for item in items:
            identity = openvpn_proxy_identity(item.name, item.server, item.port)
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            records.append(_to_runtime(item))
        return records

    def delete(self, proxy_id: int) -> bool:
        with Session(engine) as session:
            item = session.get(OpenVPNProxyModel, proxy_id)
            if not item:
                return False
            session.delete(item)
            session.commit()
            return True

    def toggle(self, proxy_id: int) -> bool | None:
        with Session(engine) as session:
            item = session.get(OpenVPNProxyModel, proxy_id)
            if not item:
                return None
            item.is_active = not item.is_active
            session.add(item)
            session.commit()
            return bool(item.is_active)

    def report_success(self, proxy_id: int) -> None:
        self._report(proxy_id, success=True)

    def report_fail(self, proxy_id: int) -> None:
        self._report(proxy_id, success=False)

    def _report(self, proxy_id: int, *, success: bool) -> None:
        threshold = int(os.getenv("MIHOMO_FAILURE_THRESHOLD", "5"))
        with Session(engine) as session:
            item = session.get(OpenVPNProxyModel, proxy_id)
            if not item:
                return
            if success:
                item.success_count += 1
            else:
                item.fail_count += 1
                if item.success_count == 0 and item.fail_count >= threshold:
                    item.is_active = False
            item.last_checked = datetime.now(timezone.utc)
            session.add(item)
            session.commit()
