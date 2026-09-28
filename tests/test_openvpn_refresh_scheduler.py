from __future__ import annotations

from datetime import datetime, timezone

from application.openvpn_refresh_scheduler import OpenVPNRefreshScheduler


class FakeRepository:
    def __init__(self):
        self.attempts: dict[str, int] = {}
        self.successful: set[str] = set()
        self.interrupted_calls = 0

    def refresh_attempt_count(self, scheduled_for: str) -> int:
        return self.attempts.get(scheduled_for, 0)

    def refresh_succeeded(self, scheduled_for: str) -> bool:
        return scheduled_for in self.successful

    def mark_interrupted_refresh_runs(self):
        self.interrupted_calls += 1
        return 0

    def begin_refresh_run(self, scheduled_for, _source, _attempt):
        self.attempts[scheduled_for] = self.attempts.get(scheduled_for, 0) + 1
        return scheduled_for

    def finish_refresh_run(self, run_id, **kwargs):
        if kwargs.get("status") == "success":
            self.successful.add(run_id)
        return None


class FakeService:
    def __init__(self, repository, statuses):
        self.repository = repository
        self.statuses = iter(statuses)
        self.calls = []

    def refresh_once(self, scheduled_for):
        self.calls.append(scheduled_for)
        self.repository.attempts[scheduled_for] = self.repository.attempts.get(scheduled_for, 0) + 1
        status = next(self.statuses)
        if status == "success":
            self.repository.successful.add(scheduled_for)
        return {"status": status}


def test_scheduler_runs_at_local_midnight_and_retries_failed_attempts():
    repository = FakeRepository()
    service = FakeService(repository, ["failed", "degraded", "success"])
    scheduler = OpenVPNRefreshScheduler(
        service=service,
        repository=repository,
        timezone_name="Asia/Shanghai",
        refresh_time="00:00",
        enabled=True,
    )

    first = scheduler.tick(datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc))
    assert first["status"] == "failed"
    assert scheduler.tick(datetime(2026, 9, 27, 16, 30, tzinfo=timezone.utc)) is None
    second = scheduler.tick(datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc))
    assert second["status"] == "degraded"
    third = scheduler.tick(datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc))
    assert third["status"] == "success"
    assert scheduler.tick(datetime(2026, 9, 27, 19, 0, tzinfo=timezone.utc)) is None
    assert len(service.calls) == 3
    assert all(call.startswith("2026-09-28T00:00:00+08:00") for call in service.calls)


def test_scheduler_does_not_retry_after_success():
    repository = FakeRepository()
    service = FakeService(repository, ["success"])
    scheduler = OpenVPNRefreshScheduler(
        service=service,
        repository=repository,
        timezone_name="Asia/Shanghai",
        refresh_time="00:00",
        enabled=True,
    )

    assert scheduler.tick(datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc))["status"] == "success"
    assert scheduler.tick(datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)) is None
    assert len(service.calls) == 1


def test_scheduler_catches_up_after_midnight_without_waiting_for_next_day():
    repository = FakeRepository()
    service = FakeService(repository, ["success"])
    scheduler = OpenVPNRefreshScheduler(
        service=service,
        repository=repository,
        timezone_name="Asia/Shanghai",
        refresh_time="00:00",
        enabled=True,
    )

    result = scheduler.tick(datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc))

    assert result["status"] == "success"
    assert service.calls == ["2026-09-28T00:00:00+08:00"]


def test_disabled_scheduler_still_recovers_interrupted_runs():
    repository = FakeRepository()
    scheduler = OpenVPNRefreshScheduler(
        service=FakeService(repository, ["success"]),
        repository=repository,
        enabled=False,
    )

    scheduler.start()

    assert repository.interrupted_calls == 1


def test_scheduler_stop_joins_worker_thread():
    import threading
    import time

    scheduler = OpenVPNRefreshScheduler(
        service=FakeService(FakeRepository(), ["success"]),
        repository=FakeRepository(),
        enabled=False,
    )
    worker = threading.Thread(target=lambda: time.sleep(0.05))
    scheduler._thread = worker
    worker.start()

    scheduler.stop()

    assert not worker.is_alive()


def test_scheduler_starts_after_midnight_when_process_restarts():
    repository = FakeRepository()
    service = FakeService(repository, ["success"])
    scheduler = OpenVPNRefreshScheduler(
        service=service,
        repository=repository,
        timezone_name="Asia/Shanghai",
        refresh_time="00:00",
        enabled=True,
    )

    result = scheduler.tick(datetime(2026, 9, 27, 16, 1, tzinfo=timezone.utc))

    assert result["status"] == "success"
    assert service.calls == ["2026-09-28T00:00:00+08:00"]


def test_scheduler_records_invalid_timezone_without_fallback():
    repository = FakeRepository()
    scheduler = OpenVPNRefreshScheduler(
        service=FakeService(repository, ["success"]),
        repository=repository,
        timezone_name="Not/AZone",
        enabled=True,
    )

    result = scheduler.tick(datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert result["reason"] == "invalid-timezone"
