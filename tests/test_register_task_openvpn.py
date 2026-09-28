from __future__ import annotations

from dataclasses import dataclass

import pytest

from application.openvpn_task import OpenVPNTaskSession
from tests.test_register_task_strategy import world


@dataclass
class FakeProfile:
    id: int
    name: str = "node"
    server: str = "198.51.100.10"
    port: int = 443


class FakeRuntime:
    proxy_url = "http://127.0.0.1:38123"

    def __init__(self):
        self.selected = []
        self.reported = []
        self.released = False

    def start_task(self):
        return self.proxy_url

    def select_for_cycle(self):
        profile = FakeProfile(len(self.selected) + 1, f"node-{len(self.selected) + 1}")
        self.selected.append(profile)
        return profile

    def report_cycle(self, success):
        self.reported.append(success)

    def release(self):
        self.released = True


class RecordingOpenVPNSession:
    proxy_url = "http://127.0.0.1:38123"

    def __init__(self, events=None, *, report_error=None):
        self.events = events if events is not None else []
        self.report_error = report_error
        self.selected = []
        self.reported = []
        self.released = 0

    def start(self):
        self.events.append("start")
        return self.proxy_url

    def select_for_cycle(self):
        selected = FakeProfile(len(self.selected) + 1, f"node-{len(self.selected) + 1}")
        self.selected.append(selected)
        self.events.append(("select", selected.name))
        return selected

    def report_cycle(self, success):
        self.reported.append(success)
        self.events.append(("report", success))
        if self.report_error is not None:
            raise self.report_error

    def release(self):
        self.released += 1
        self.events.append("release")


class FailingInitialSelectionSession(RecordingOpenVPNSession):
    def select_for_cycle(self):
        self.events.append("select")
        raise RuntimeError("first node unavailable")


def test_execute_register_task_preselects_before_mailbox_and_reuses_node_for_retries(monkeypatch):
    events = []
    world_instance = world(monkeypatch, outcomes=["boom", "ok"], proxies=[])
    session = RecordingOpenVPNSession(events)
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)
    monkeypatch.setattr(
        "core.base_mailbox.create_mailbox",
        lambda **kwargs: events.append("mailbox") or object(),
    )

    task = world_instance.run(proxy_mode="openvpn", count=1, retry_count=1)

    assert [item.name for item in session.selected] == ["node-1"]
    assert events.index(("select", "node-1")) < events.index("mailbox")
    assert world_instance.proxies_used == [session.proxy_url, session.proxy_url]
    assert session.reported == [True]
    assert session.released == 1
    assert task["status"] == "succeeded"


def test_execute_register_task_selects_once_per_later_cycle_and_retries_keep_node(monkeypatch):
    world_instance = world(
        monkeypatch,
        outcomes=["boom", "ok", "boom", "ok"],
        proxies=[],
    )
    session = RecordingOpenVPNSession()
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)

    world_instance.run(proxy_mode="openvpn", count=2, retry_count=1)

    assert [item.name for item in session.selected] == ["node-1", "node-2"]
    assert session.reported == [True, True]
    assert session.released == 1
    assert world_instance.proxies_used == [session.proxy_url] * 4


def test_execute_register_task_initial_selection_failure_skips_mailbox_and_finishes_with_openvpn_error(monkeypatch):
    events = []
    world_instance = world(monkeypatch, outcomes=["ok"], proxies=[])
    session = FailingInitialSelectionSession(events)
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)
    monkeypatch.setattr(
        "core.base_mailbox.create_mailbox",
        lambda **kwargs: events.append("mailbox") or object(),
    )

    task = world_instance.run(proxy_mode="openvpn", count=1)

    assert world_instance.register_calls == []
    assert "mailbox" not in events
    assert session.released == 1
    assert task["status"] == "failed"
    assert "OpenVPN" in (task["error"] or "")


def test_openvpn_reporting_callback_failure_is_warning_only(monkeypatch):
    world_instance = world(monkeypatch, outcomes=["ok"], proxies=[])
    session = RecordingOpenVPNSession(report_error=RuntimeError("statistics unavailable"))
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)

    task = world_instance.run(proxy_mode="openvpn", count=1)

    assert task["status"] == "succeeded"
    assert task["error"] in (None, "")
    assert world_instance.proxies_used == [session.proxy_url]
    assert session.reported == [True]
    assert session.released == 1


def test_openvpn_mode_requires_runtime_proxy_before_registration(monkeypatch):
    world_instance = world(monkeypatch, outcomes=["ok"], proxies=[])
    session = RecordingOpenVPNSession()
    session.proxy_url = ""
    session.start = lambda: ""
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)

    world_instance.run(proxy_mode="openvpn", count=1)

    assert world_instance.register_calls == []
    assert session.released == 1


def test_openvpn_runtime_is_released_when_registration_exhausts(monkeypatch):
    world_instance = world(monkeypatch, outcomes=[], proxies=[])
    session = RecordingOpenVPNSession()
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)

    world_instance.run(proxy_mode="openvpn", count=1, retry_count=0, max_attempts=1)

    assert session.released == 1


def test_openvpn_session_selects_once_per_cycle_and_reuses_local_proxy():
    runtime = FakeRuntime()
    session = OpenVPNTaskSession(runtime)

    assert session.start() == "http://127.0.0.1:38123"
    first = session.select_for_cycle()
    session.report_cycle(True)
    second = session.select_for_cycle()
    session.report_cycle(False)

    assert first.name == "node-1"
    assert second.name == "node-2"
    assert runtime.selected == [first, second]
    assert runtime.reported == [True, False]


def test_openvpn_session_releases_runtime_when_start_fails():
    runtime = FakeRuntime()
    runtime.start_task = lambda: (_ for _ in ()).throw(RuntimeError("mihomo unavailable"))
    session = OpenVPNTaskSession(runtime)

    try:
        session.start()
    except RuntimeError:
        pass

    assert runtime.released is True


def test_mailbox_close_attribute_error_does_not_skip_openvpn_release(monkeypatch):
    world_instance = world(monkeypatch, outcomes=["ok"], proxies=[])
    session = RecordingOpenVPNSession()
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)

    class BrokenMailbox:
        @property
        def close(self):
            raise RuntimeError("mailbox close lookup failed")

    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **_kwargs: BrokenMailbox())

    task = world_instance.run(proxy_mode="openvpn", count=1)

    assert task["status"] == "succeeded"
    assert session.released == 1


def test_failed_account_cleanup_error_does_not_skip_openvpn_release(monkeypatch):
    world_instance = world(monkeypatch, outcomes=["ok"], proxies=[])
    session = RecordingOpenVPNSession()
    monkeypatch.setattr("application.tasks._create_openvpn_task_session", lambda *_args: session)
    monkeypatch.setattr(
        "application.tasks._claim_failed_account",
        lambda *_args, **_kwargs: {
            "email": "reused@example.com",
            "password": "pw",
            "failure_stage": "not_created",
            "failure_reason": "",
            "mail_provider": "",
        },
    )
    monkeypatch.setattr(
        "application.tasks._release_failed_account",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("failed account cleanup failed")),
    )
    monkeypatch.setattr("application.tasks.list_reusable_failed_accounts", lambda *_args: [])

    task = world_instance.run(proxy_mode="openvpn", count=1)

    assert task["status"] == "succeeded"
    assert session.released == 1


def _create_session_with_candidate_limit(monkeypatch, value=None):
    from application.tasks import _create_openvpn_task_session

    class FakeRepository:
        def active_runtime_records(self):
            return [FakeProfile(7)]

    monkeypatch.setattr(
        "infrastructure.openvpn_proxies_repository.OpenVPNProxiesRepository",
        FakeRepository,
    )
    if value is None:
        monkeypatch.delenv("MIHOMO_MAX_CANDIDATES", raising=False)
    else:
        monkeypatch.setenv("MIHOMO_MAX_CANDIDATES", value)
    return _create_openvpn_task_session()


def test_openvpn_session_uses_default_five_candidate_limit(monkeypatch):
    session = _create_session_with_candidate_limit(monkeypatch)

    assert session.runtime.max_candidates == 5


def test_openvpn_session_zero_candidate_limit_is_unlimited(monkeypatch):
    session = _create_session_with_candidate_limit(monkeypatch, "0")

    assert session.runtime.max_candidates is None


def test_openvpn_session_rejects_negative_candidate_limit(monkeypatch):
    with pytest.raises(ValueError, match="max_candidates"):
        _create_session_with_candidate_limit(monkeypatch, "-1")


def test_repository_stats_callback_logs_but_does_not_raise(monkeypatch):
    from application.tasks import _create_openvpn_task_session

    profile = FakeProfile(7)
    logs = []

    class FakeRepository:
        def active_runtime_records(self):
            return [profile]

        def report_success(self, _proxy_id):
            raise RuntimeError("sqlite locked")

        def report_fail(self, _proxy_id):
            raise RuntimeError("sqlite locked")

    class FakeManager:
        report_callback = None

        def __init__(self, _profiles, **kwargs):
            type(self).report_callback = kwargs["report_callback"]

    class FakeLogger:
        def log(self, message, *, level="info"):
            logs.append((level, message))

    monkeypatch.setattr(
        "infrastructure.openvpn_proxies_repository.OpenVPNProxiesRepository",
        FakeRepository,
    )
    monkeypatch.setattr("core.openvpn_runtime.MihomoRuntimeManager", FakeManager)

    _create_openvpn_task_session(FakeLogger())
    FakeManager.report_callback(profile, True)
    FakeManager.report_callback(profile, False)

    assert len(logs) == 2
    assert all(level == "warning" for level, _message in logs)
    assert all("OpenVPN" in message for _level, message in logs)
