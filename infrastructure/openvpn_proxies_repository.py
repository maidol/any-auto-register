from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from core.db import OpenVPNProxyModel, OpenVPNRefreshRunModel, engine
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
        source=model.source,
        refresh_healthy=model.refresh_healthy,
        last_seen_at=model.last_seen_at,
        last_refresh_checked_at=model.last_refresh_checked_at,
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
                    reclaim_refresh_ownership = existing.source == "vpngate"
                    unchanged = (
                        not reclaim_refresh_ownership
                        and existing.name == name
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
                    existing.source = "manual"
                    existing.refresh_healthy = None
                    existing.last_seen_at = None
                    existing.last_refresh_checked_at = None
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
                    source="manual",
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
                            "source": "manual",
                            "refresh_healthy": None,
                            "last_seen_at": None,
                            "last_refresh_checked_at": None,
                        },
                    )
                )
                summary.added += 1
            session.commit()
        return summary

    def begin_refresh_run(self, scheduled_for: str, source: str, attempt_number: int) -> int:
        run = OpenVPNRefreshRunModel(
            scheduled_for=scheduled_for,
            source=source,
            attempt_number=int(attempt_number),
        )
        with Session(engine) as session:
            session.add(run)
            session.commit()
            session.refresh(run)
        return int(run.id or 0)

    def mark_interrupted_refresh_runs(self) -> int:
        updated = 0
        with Session(engine) as session:
            runs = session.exec(
                select(OpenVPNRefreshRunModel).where(OpenVPNRefreshRunModel.status == "running")
            ).all()
            for run in runs:
                run.status = "interrupted"
                run.finished_at = datetime.now(timezone.utc)
                session.add(run)
                updated += 1
            session.commit()
        try:
            self._prune_refresh_runs()
        except Exception:
            pass
        return updated

    def refresh_attempt_count(self, scheduled_for: str) -> int:
        with Session(engine) as session:
            return len(
                session.exec(
                    select(OpenVPNRefreshRunModel).where(
                        OpenVPNRefreshRunModel.scheduled_for == scheduled_for
                    )
                ).all()
            )

    def refresh_succeeded(self, scheduled_for: str) -> bool:
        with Session(engine) as session:
            return session.exec(
                select(OpenVPNRefreshRunModel).where(
                    OpenVPNRefreshRunModel.scheduled_for == scheduled_for,
                    OpenVPNRefreshRunModel.status == "success",
                )
            ).first() is not None

    def finish_refresh_run(
        self,
        run_id: int,
        *,
        status: str,
        snapshot_hash: str = "",
        source_row_count: int = 0,
        convertible_count: int = 0,
        checked_count: int = 0,
        healthy_count: int = 0,
        failures: list[dict[str, object]] | None = None,
    ) -> None:
        with Session(engine) as session:
            run = session.get(OpenVPNRefreshRunModel, run_id)
            if not run:
                return
            run.status = status
            run.finished_at = datetime.now(timezone.utc)
            run.snapshot_hash = snapshot_hash
            run.source_row_count = int(source_row_count)
            run.convertible_count = int(convertible_count)
            run.checked_count = int(checked_count)
            run.healthy_count = int(healthy_count)
            run.failure_count = len(failures or [])
            run.failures_json = json.dumps(failures or [], ensure_ascii=False)
            session.add(run)
            session.commit()
        try:
            self._prune_refresh_runs()
        except Exception:
            pass

    def manual_identity_collisions(self, identities: list[str]) -> list[dict[str, object]]:
        if not identities:
            return []
        with Session(engine) as session:
            rows = session.exec(
                select(OpenVPNProxyModel).where(
                    OpenVPNProxyModel.identity.in_(identities),
                    OpenVPNProxyModel.source != "vpngate",
                )
            ).all()
        return [
            {"identity": item.identity, "reason": "manual_identity_collision"}
            for item in rows
        ]

    def commit_refresh(
        self,
        run_id: int,
        outcomes: list[dict[str, object]],
        *,
        checked_at: datetime,
        snapshot_hash: str,
        source_row_count: int,
        convertible_count: int,
        failures: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        collision_failures: list[dict[str, object]] = []
        seen_identities: set[str] = set()
        with Session(engine) as session:
            for outcome in outcomes:
                entry = dict(outcome["entry"])
                identity = str(outcome["identity"])
                healthy = bool(outcome["healthy"])
                region = str(outcome.get("region") or "")
                seen_identities.add(identity)
                config_json = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                existing = session.exec(
                    select(OpenVPNProxyModel).where(OpenVPNProxyModel.identity == identity)
                ).first()
                if existing is not None and existing.source != "vpngate":
                    collision_failures.append(
                        {
                            "identity": identity,
                            "reason": "manual_identity_collision",
                        }
                    )
                    continue
                if existing is None:
                    existing = OpenVPNProxyModel(
                        name=str(entry["name"]),
                        server=str(entry["server"]),
                        port=int(entry["port"]),
                        identity=identity,
                        proto=str(entry.get("proto") or "udp"),
                        mihomo_config_json=config_json,
                        region=region,
                        source="vpngate",
                        refresh_healthy=healthy,
                        last_seen_at=checked_at,
                        last_refresh_checked_at=checked_at,
                    )
                else:
                    existing.name = str(entry["name"])
                    existing.server = str(entry["server"])
                    existing.port = int(entry["port"])
                    existing.proto = str(entry.get("proto") or "udp")
                    existing.mihomo_config_json = config_json
                    existing.region = region
                    existing.refresh_healthy = healthy
                    existing.last_seen_at = checked_at
                    existing.last_refresh_checked_at = checked_at
                session.add(existing)

            old_vpngate = session.exec(
                select(OpenVPNProxyModel).where(OpenVPNProxyModel.source == "vpngate")
            ).all()
            for item in old_vpngate:
                identity = openvpn_proxy_identity(item.name, item.server, item.port)
                if identity not in seen_identities:
                    item.refresh_healthy = False
                    item.last_refresh_checked_at = checked_at
                    session.add(item)

            run = session.get(OpenVPNRefreshRunModel, run_id)
            if run is None:
                raise RuntimeError("OpenVPN refresh audit run is missing")
            all_failures = list(failures) + collision_failures
            run.status = "success"
            run.finished_at = datetime.now(timezone.utc)
            run.snapshot_hash = snapshot_hash
            run.source_row_count = int(source_row_count)
            run.convertible_count = int(convertible_count)
            run.checked_count = len(outcomes)
            run.healthy_count = sum(1 for item in outcomes if item["healthy"])
            run.failure_count = len(all_failures)
            run.failures_json = json.dumps(all_failures, ensure_ascii=False)
            session.add(run)
            session.commit()
        try:
            self._prune_refresh_runs()
        except Exception:
            pass
        return collision_failures

    def list_refresh_runs(self, limit: int = 30) -> list[OpenVPNRefreshRunModel]:
        with Session(engine) as session:
            return list(
                session.exec(
                    select(OpenVPNRefreshRunModel)
                    .order_by(OpenVPNRefreshRunModel.id.desc())
                    .limit(int(limit))
                ).all()
            )

    def _prune_refresh_runs(self, keep: int = 30) -> None:
        with Session(engine) as session:
            runs = list(
                session.exec(
                    select(OpenVPNRefreshRunModel).order_by(OpenVPNRefreshRunModel.id.desc())
                ).all()
            )
            for run in runs[int(keep) :]:
                session.delete(run)
            session.commit()

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
                .where(
                    (OpenVPNProxyModel.source != "vpngate")
                    | (OpenVPNProxyModel.refresh_healthy == True)
                )
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
