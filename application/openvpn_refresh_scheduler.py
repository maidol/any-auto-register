"""Interruptible daily scheduler for VPN Gate OpenVPN refreshes."""

from __future__ import annotations

import os
import threading
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from application.openvpn_refresh import OpenVPNRefreshService, openvpn_refresh_service
from infrastructure.openvpn_proxies_repository import OpenVPNProxiesRepository


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_REFRESH_TIME = "00:00"
DEFAULT_MAX_ATTEMPTS = 3


class OpenVPNRefreshScheduler:
    def __init__(
        self,
        service: OpenVPNRefreshService | None = None,
        repository: OpenVPNProxiesRepository | None = None,
        *,
        timezone_name: str | None = None,
        refresh_time: str | None = None,
        enabled: bool | None = None,
        max_attempts: int | None = None,
    ):
        timezone_name = timezone_name or os.getenv("VPN_GATE_REFRESH_TIMEZONE", DEFAULT_TIMEZONE)
        self.timezone_error: str | None = None
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self.timezone = None
            self.timezone_error = f"invalid timezone VPN_GATE_REFRESH_TIMEZONE: {timezone_name}"
        raw_time = refresh_time or os.getenv("VPN_GATE_REFRESH_TIME", DEFAULT_REFRESH_TIME)
        self.refresh_time_error: str | None = None
        try:
            hour_text, minute_text = raw_time.split(":", 1)
            self.refresh_time = time(int(hour_text), int(minute_text))
        except (ValueError, TypeError) as exc:
            self.refresh_time = time(0, 0)
            self.refresh_time_error = f"invalid VPN_GATE_REFRESH_TIME: {raw_time}"
        if self.refresh_time.hour > 23 or self.refresh_time.minute > 59:
            self.refresh_time_error = f"invalid VPN_GATE_REFRESH_TIME: {raw_time}"
        self.service = service or openvpn_refresh_service
        self.repository = repository or self.service.repository
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("VPN_GATE_REFRESH_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        raw_attempts = max_attempts
        if raw_attempts is None:
            raw_attempts = os.getenv("VPN_GATE_REFRESH_RETRIES", str(DEFAULT_MAX_ATTEMPTS))
        try:
            raw_attempts = int(raw_attempts)
        except (TypeError, ValueError) as exc:
            raise ValueError("VPN_GATE_REFRESH_RETRIES must be an integer") from exc
        if raw_attempts <= 0:
            raise ValueError("VPN_GATE_REFRESH_RETRIES must be positive")
        self.max_attempts = raw_attempts
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.repository.mark_interrupted_refresh_runs()
        if not self.enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="openvpn-refresh",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        if thread is None or not thread.is_alive():
            self._thread = None

    def tick(self, now: datetime) -> dict[str, object] | None:
        if not self.enabled:
            return None
        config_error = self.timezone_error or self.refresh_time_error
        if config_error:
            error_kind = "invalid-timezone" if self.timezone_error else "invalid-refresh-time"
            scheduled_for = f"{error_kind}:{config_error}"
            attempts = self.repository.refresh_attempt_count(scheduled_for)
            if attempts >= self.max_attempts:
                return None
            run_id = self.repository.begin_refresh_run(
                scheduled_for,
                getattr(self.service, "source", os.getenv("VPN_GATE_SOURCE", "")),
                attempts + 1,
            )
            failure = {"reason": error_kind, "detail": config_error}
            self.repository.finish_refresh_run(run_id, status="failed", failures=[failure])
            return {"status": "failed", "reason": error_kind, "failures": [failure]}
        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        now_local = now_utc.astimezone(self.timezone)
        today = self._today_target(now_local)
        scheduled = today
        scheduled_for = scheduled.isoformat()
        if now_local < scheduled:
            return None
        if self.repository.refresh_succeeded(scheduled_for):
            return None
        attempts = self.repository.refresh_attempt_count(scheduled_for)
        if attempts >= self.max_attempts:
            return None
        retry_at = scheduled + timedelta(hours=attempts)
        if now_local < retry_at:
            return None
        return self.service.refresh_once(scheduled_for=scheduled_for)

    def _today_target(self, now_local: datetime) -> datetime:
        return now_local.replace(
            hour=self.refresh_time.hour,
            minute=self.refresh_time.minute,
            second=0,
            microsecond=0,
        )

    def _loop(self) -> None:
        while not self._stop.wait(60.0):
            try:
                self.tick(datetime.now(timezone.utc))
            except Exception as exc:
                print(f"[OpenVPN] 定时刷新失败: {exc}")


openvpn_refresh_scheduler = OpenVPNRefreshScheduler()
