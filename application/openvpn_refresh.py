"""VPN Gate OpenVPN refresh and health-snapshot orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from core.openvpn_runtime import MihomoRuntimeManager
from domain.openvpn_proxies import OpenVPNProbeResult, OpenVPNProxyRuntimeRecord, openvpn_proxy_identity
from infrastructure.openvpn_proxies_repository import OpenVPNProxiesRepository
from tools.vpngate_openvpn_export import build_mihomo_proxy, fetch_snapshot, parse_snapshot


DEFAULT_VPN_GATE_SOURCE = "https://www.vpngate.net/api/iphone/"
DEFAULT_HEALTH_URL = "https://www.gstatic.com/generate_204"


def positive_env_number(name: str, default: int | float, cast: Callable[[str], int | float]) -> int | float:
    """读一个必须为正数的环境变量；写坏了只打警告并回落默认值，不让应用起不来。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or value <= 0:
        print(f"[OpenVPN] 环境变量 {name}={raw!r} 无效，改用默认值 {default}")
        return default
    return value


class RefreshBusyError(RuntimeError):
    """Raised when a refresh is already running in this process."""


class OpenVPNRefreshService:
    def __init__(
        self,
        repository: OpenVPNProxiesRepository | None = None,
        *,
        manager_factory: Callable[..., MihomoRuntimeManager] = MihomoRuntimeManager,
        snapshot_fetcher: Callable[[str, int], str] = fetch_snapshot,
        snapshot_parser: Callable[[str], tuple[list[dict[str, object]], list[dict[str, str]]]] = parse_snapshot,
        proxy_builder: Callable[[dict[str, object]], dict[str, object]] = build_mihomo_proxy,
        direct_probe: Callable[[str], bool] | None = None,
        source: str | None = None,
        health_url: str | None = None,
        fetch_timeout: int | None = None,
        health_timeout: float | None = None,
        max_snapshot_bytes: int | None = None,
    ):
        self.repository = repository or OpenVPNProxiesRepository()
        self.manager_factory = manager_factory
        self.snapshot_fetcher = snapshot_fetcher
        self._uses_default_snapshot_fetcher = snapshot_fetcher is fetch_snapshot
        if max_snapshot_bytes:
            try:
                self.max_snapshot_bytes = int(max_snapshot_bytes)
            except (TypeError, ValueError) as exc:
                raise ValueError("VPN_GATE_MAX_SNAPSHOT_BYTES must be an integer") from exc
            if self.max_snapshot_bytes <= 0:
                raise ValueError("VPN_GATE_MAX_SNAPSHOT_BYTES must be positive")
        else:
            self.max_snapshot_bytes = positive_env_number("VPN_GATE_MAX_SNAPSHOT_BYTES", 12582912, int)
        self.snapshot_parser = snapshot_parser
        self.proxy_builder = proxy_builder
        self.source = source or os.getenv("VPN_GATE_SOURCE", DEFAULT_VPN_GATE_SOURCE)
        self.health_url = health_url or os.getenv("VPN_GATE_HEALTHCHECK_URL", DEFAULT_HEALTH_URL)
        self.fetch_timeout = int(fetch_timeout or positive_env_number("VPN_GATE_FETCH_TIMEOUT", 30, int))
        self.health_timeout = float(
            health_timeout or positive_env_number("VPN_GATE_HEALTHCHECK_TIMEOUT", 20.0, float)
        )
        self._direct_probe = direct_probe or self._probe_direct
        self._lock = threading.Lock()

    @staticmethod
    def _safe_failures(failures: list[dict[str, object]]) -> list[dict[str, object]]:
        safe: list[dict[str, object]] = []
        for failure in failures:
            text = str(failure.get("reason") or "").lower()
            if "invalid timezone" in text or "invalid-timezone" in text:
                reason = "invalid-timezone"
            elif "invalid vpn_gate_refresh_time" in text or "invalid-refresh-time" in text:
                reason = "invalid-refresh-time"
            elif "direct-probe" in text:
                reason = "direct-probe-failed"
            elif "manual_identity_collision" in text:
                reason = "manual_identity_collision"
            elif "duplicate" in text:
                reason = "duplicate_identity"
            elif "controller" in text:
                reason = "controller-selection-failed"
            elif "timeout" in text:
                reason = "proxy-timeout"
            elif "no-healthy" in text:
                reason = "no-healthy-candidates"
            elif "health" in text:
                reason = "proxy-health-failed"
            elif "mihomo" in text or "process" in text:
                reason = "mihomo-runtime-failed"
            elif any(word in text for word in ("invalid", "unsupported", "missing", "empty")):
                reason = "profile-invalid"
            elif "convertible" in text:
                reason = "no-convertible-candidates"
            else:
                reason = "refresh-failed"
            item: dict[str, object] = {"reason": reason}
            for key in ("row_number", "host_name", "ip", "identity", "phase"):
                if failure.get(key) not in (None, ""):
                    item[key] = failure[key]
            safe.append(item)
        return safe

    def refresh_once(self, scheduled_for: str | None = None) -> dict[str, object]:
        if not self._lock.acquire(blocking=False):
            raise RefreshBusyError("OpenVPN refresh is already running")

        requested_scheduled_for = scheduled_for
        try:
            if scheduled_for is None:
                try:
                    scheduled_for = self._default_scheduled_for()
                except ValueError as exc:
                    error_text = str(exc)
                    scheduled_for = f"invalid-config:{error_text}"
                    run_id = self.repository.begin_refresh_run(scheduled_for, self.source, 1)
                    failure_reason = (
                        "invalid-timezone" if "timezone" in error_text.lower() else "invalid-refresh-time"
                    )
                    failure = {"reason": failure_reason, "phase": "configuration"}
                    self.repository.finish_refresh_run(
                        run_id,
                        status="failed",
                        failures=self._safe_failures([failure]),
                    )
                    self._lock.release()
                    return {
                        "status": "failed",
                        "reason": failure_reason,
                        "failures": self._safe_failures([failure]),
                    }
            if requested_scheduled_for is not None and self.repository.refresh_succeeded(scheduled_for):
                self._lock.release()
                return {"status": "skipped", "reason": "already-succeeded"}
            attempt_number = self.repository.refresh_attempt_count(scheduled_for) + 1
            run_id = self.repository.begin_refresh_run(scheduled_for, self.source, attempt_number)
        except Exception:
            self._lock.release()
            raise
        snapshot_hash = ""
        rows: list[dict[str, object]] = []
        parse_failures: list[dict[str, str]] = []
        conversion_failures: list[dict[str, object]] = []
        candidates: list[dict[str, object]] = []
        manager = None
        outcomes: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        try:
            if not self._direct_probe(self.health_url):
                failure = {"reason": "direct-probe-failed", "phase": "before"}
                self.repository.finish_refresh_run(
                    run_id,
                    status="failed",
                    failures=self._safe_failures([failure]),
                )
                return {"status": "failed", "reason": "direct-probe-failed", "failures": self._safe_failures([failure])}

            if self._uses_default_snapshot_fetcher:
                snapshot = fetch_snapshot(
                    self.source,
                    self.fetch_timeout,
                    self.max_snapshot_bytes,
                )
            else:
                snapshot = self.snapshot_fetcher(self.source, self.fetch_timeout)
            snapshot_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
            rows, parse_failures = self.snapshot_parser(snapshot)
            failures.extend(dict(item) for item in parse_failures)
            seen_identities: set[str] = set()
            for row in rows:
                try:
                    item = self.proxy_builder(row)
                    identity = openvpn_proxy_identity(
                        str(item["name"]), str(item["server"]), int(item["port"])
                    )
                except Exception as exc:
                    failure = {
                        "row_number": row.get("row_number", ""),
                        "reason": str(exc),
                    }
                    conversion_failures.append(failure)
                    failures.append(failure)
                    continue
                if identity in seen_identities:
                    failure = {
                        "row_number": row.get("row_number", ""),
                        "identity": identity,
                        "reason": "duplicate_identity",
                    }
                    conversion_failures.append(failure)
                    failures.append(failure)
                    continue
                seen_identities.add(identity)
                profile = OpenVPNProxyRuntimeRecord(
                    id=-(len(candidates) + 1),
                    name=str(item["name"]),
                    server=str(item["server"]),
                    port=int(item["port"]),
                    proto=str(item.get("proto") or "udp"),
                    config=item,
                )
                candidates.append(
                    {
                        "entry": item,
                        "identity": identity,
                        "region": str(row.get("CountryShort") or ""),
                        "profile": profile,
                    }
                )

            if not candidates:
                failure = {"reason": "no-convertible-candidates"}
                failures.append(failure)
                self.repository.finish_refresh_run(
                    run_id,
                    status="failed",
                    snapshot_hash=snapshot_hash,
                    source_row_count=len(rows) + len(parse_failures),
                    convertible_count=0,
                    failures=self._safe_failures(failures),
                )
                return {"status": "failed", "reason": "no-convertible-candidates", "failures": self._safe_failures(failures)}

            manager = self.manager_factory(
                [candidate["profile"] for candidate in candidates],
                binary=os.getenv("MIHOMO_BIN", "mihomo"),
                work_root=Path(os.getenv("MIHOMO_RUNTIME_DIR", tempfile.gettempdir())),
                start_timeout=float(os.getenv("MIHOMO_START_TIMEOUT", "20")),
                health_url=self.health_url,
                health_timeout=self.health_timeout,
                max_candidates=0,
                report_callback=None,
            )
            try:
                manager.start_task()
                probe_results = manager.probe_all()
            except Exception as exc:
                failure = {"reason": "mihomo-runtime-failed", "detail": str(exc)[:240]}
                failures.append(failure)
                self.repository.finish_refresh_run(
                    run_id,
                    status="failed",
                    snapshot_hash=snapshot_hash,
                    source_row_count=len(rows) + len(parse_failures),
                    convertible_count=len(candidates),
                    failures=self._safe_failures(failures),
                )
                return {"status": "failed", "reason": "mihomo-runtime-failed", "failures": self._safe_failures(failures)}

            by_id = {candidate["profile"].id: candidate for candidate in candidates}
            if len(probe_results) != len(candidates):
                raise RuntimeError("Mihomo probe did not return every candidate")
            seen_probe_ids: set[int] = set()
            for result in probe_results:
                if not isinstance(result, OpenVPNProbeResult):
                    raise RuntimeError("Mihomo probe returned an invalid result")
                candidate = by_id.get(result.profile.id)
                if candidate is None or result.profile.id in seen_probe_ids:
                    raise RuntimeError("Mihomo probe returned an unknown or duplicate profile")
                seen_probe_ids.add(result.profile.id)
                outcome = {
                    "entry": candidate["entry"],
                    "identity": candidate["identity"],
                    "region": candidate["region"],
                    "healthy": bool(result.healthy),
                    "reason": result.reason,
                }
                outcomes.append(outcome)
                if not result.healthy:
                    failures.append(
                        {
                            "identity": candidate["identity"],
                            "reason": result.reason or "proxy-health-failed",
                        }
                    )

            if not self._direct_probe(self.health_url):
                failure = {"reason": "direct-probe-failed", "phase": "after"}
                failures.extend(
                    self.repository.manual_identity_collisions(
                        [str(item["identity"]) for item in outcomes]
                    )
                )
                failures.append(failure)
                self.repository.finish_refresh_run(
                    run_id,
                    status="failed",
                    snapshot_hash=snapshot_hash,
                    source_row_count=len(rows) + len(parse_failures),
                    convertible_count=len(candidates),
                    checked_count=len(outcomes),
                    healthy_count=sum(1 for item in outcomes if item["healthy"]),
                    failures=self._safe_failures(failures),
                )
                return {"status": "failed", "reason": "direct-probe-failed", "failures": self._safe_failures(failures)}

            healthy_count = sum(1 for item in outcomes if item["healthy"])
            if healthy_count == 0:
                failures.extend(
                    self.repository.manual_identity_collisions(
                        [str(item["identity"]) for item in outcomes]
                    )
                )
                failure = {"reason": "no-healthy-candidates"}
                failures.append(failure)
                self.repository.finish_refresh_run(
                    run_id,
                    status="degraded",
                    snapshot_hash=snapshot_hash,
                    source_row_count=len(rows) + len(parse_failures),
                    convertible_count=len(candidates),
                    checked_count=len(outcomes),
                    healthy_count=0,
                    failures=self._safe_failures(failures),
                )
                return {"status": "degraded", "reason": "no-healthy-candidates", "failures": self._safe_failures(failures)}

            checked_at = datetime.now(timezone.utc)
            collision_failures = self.repository.commit_refresh(
                run_id,
                outcomes,
                checked_at=checked_at,
                snapshot_hash=snapshot_hash,
                source_row_count=len(rows) + len(parse_failures),
                convertible_count=len(candidates),
                failures=self._safe_failures(failures),
            )
            failures.extend(collision_failures)
            return {
                "status": "success",
                "checked_count": len(outcomes),
                "healthy_count": healthy_count,
                "failures": self._safe_failures(failures),
            }
        except Exception as exc:
            failure = {"reason": "refresh-failed", "detail": str(exc)[:240]}
            try:
                self.repository.finish_refresh_run(
                    run_id,
                    status="failed",
                    snapshot_hash=snapshot_hash,
                    source_row_count=len(rows) + len(parse_failures),
                    convertible_count=len(candidates),
                    checked_count=len(outcomes),
                    healthy_count=sum(1 for item in outcomes if item["healthy"]),
                    failures=self._safe_failures([*failures, failure]),
                )
            except Exception:
                pass
            return {
                "status": "failed",
                "reason": "refresh-failed",
                "failures": self._safe_failures([*failures, failure]),
            }
        finally:
            if manager is not None:
                try:
                    manager.release()
                except Exception:
                    pass
            self._lock.release()

    def _default_scheduled_for(self) -> str:
        timezone_name = os.getenv("VPN_GATE_REFRESH_TIMEZONE", "Asia/Shanghai")
        try:
            local_tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"invalid timezone VPN_GATE_REFRESH_TIMEZONE: {timezone_name}") from exc
        now_local = datetime.now(local_tz)
        raw_time = os.getenv("VPN_GATE_REFRESH_TIME", "00:00")
        try:
            hour, minute = (int(part) for part in raw_time.split(":", 1))
            scheduled = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid VPN_GATE_REFRESH_TIME: {raw_time}") from exc
        if now_local < scheduled:
            scheduled -= timedelta(days=1)
        return scheduled.isoformat()

    def list_runs(self, limit: int = 30) -> list[dict[str, object]]:
        runs = self.repository.list_refresh_runs(limit=limit)
        result = []
        for run in runs:
            try:
                failures = json.loads(run.failures_json or "[]")
            except (TypeError, ValueError):
                failures = []
            result.append(
                {
                    "id": run.id,
                    "scheduled_for": run.scheduled_for,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "status": run.status,
                    "source": run.source,
                    "snapshot_hash": run.snapshot_hash,
                    "source_row_count": run.source_row_count,
                    "convertible_count": run.convertible_count,
                    "checked_count": run.checked_count,
                    "healthy_count": run.healthy_count,
                    "failure_count": run.failure_count,
                    "attempt_number": run.attempt_number,
                    "failures": self._safe_failures(failures),
                }
            )
        return result

    def _probe_direct(self, url: str) -> bool:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(url, timeout=self.health_timeout)
            return 200 <= response.status_code < 400
        except requests.RequestException:
            return False
        finally:
            session.close()


openvpn_refresh_service = OpenVPNRefreshService()
