from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from application.openvpn_task import OpenVPNTaskSession
from core.openvpn_config import render_single_openvpn_config
from core.openvpn_runtime import MihomoRuntimeManager
from domain.openvpn_proxies import OpenVPNProxyRuntimeRecord


CA = "-----BEGIN CERTIFICATE-----\nQ0E=\n-----END CERTIFICATE-----"
CERT = "-----BEGIN CERTIFICATE-----\nQ0V=\n-----END CERTIFICATE-----"
KEY = "-----BEGIN PRIVATE KEY-----\nS0U=\n-----END PRIVATE KEY-----"


def runtime_record(proxy_id: int, name: str) -> OpenVPNProxyRuntimeRecord:
    return OpenVPNProxyRuntimeRecord(
        id=proxy_id,
        name=name,
        server=f"198.51.100.{proxy_id}",
        port=443,
        proto="tcp",
        config={
            "name": name,
            "type": "openvpn",
            "server": f"198.51.100.{proxy_id}",
            "port": 443,
            "proto": "tcp",
            "udp": False,
            "ca": CA,
            "cert": CERT,
            "key": KEY,
        },
    )


class FakeProcess:
    def __init__(self, *_args, **_kwargs):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeController:
    def __init__(self, *_args, **_kwargs):
        self.selected: list[str] = []

    def wait_ready(self, timeout):
        return True

    def select(self, group_name: str, proxy_name: str):
        assert group_name == "VPNGate"
        self.selected.append(proxy_name)


def build_manager(
    tmp_path: Path,
    health_results=None,
    callback=None,
    profiles=None,
    mixed_port_checker=None,
    controller=None,
    process_factory=FakeProcess,
    **kwargs,
):
    results = iter(health_results or [True])
    controller = controller or FakeController()

    def health_checker(_proxy_url):
        return next(results)

    return MihomoRuntimeManager(
        profiles=profiles or [runtime_record(1, "first"), runtime_record(2, "second")],
        binary="mihomo",
        work_root=tmp_path,
        process_factory=process_factory,
        controller_factory=lambda *_args, **_kwargs: controller,
        health_checker=health_checker,
        mixed_port_checker=mixed_port_checker or (lambda _port, _timeout: True),
        report_callback=callback,
        randomizer=lambda items: items,
        **kwargs,
    ), controller


def test_runtime_assigns_unique_mihomo_names_for_duplicate_display_names(tmp_path):
    profiles = [runtime_record(1, "shared"), runtime_record(2, "shared")]
    manager, controller = build_manager(tmp_path, profiles=profiles)
    manager.randomizer = lambda items: items.reverse()

    manager.start_task()
    config = yaml.safe_load(manager.config_path.read_text(encoding="utf-8"))
    runtime_names = [entry["name"] for entry in config["proxies"]]

    assert len(set(runtime_names)) == 2
    assert profiles[0].name == profiles[1].name == "shared"
    assert config["proxy-groups"][0]["proxies"] == runtime_names

    selected = manager.select_for_cycle()

    assert selected is profiles[1]
    assert controller.selected == [runtime_names[1]]


def test_runtime_config_routes_all_requests_to_vpngate_group():
    parsed = yaml.safe_load(
        render_single_openvpn_config(
            [runtime_record(1, "first").config], mixed_port=12345, controller="127.0.0.1:12346"
        )
    )

    assert parsed["rules"] == ["MATCH,VPNGate"]


def test_runtime_starts_with_isolated_ports_and_returns_local_http_proxy(tmp_path):
    manager, _controller = build_manager(tmp_path)

    proxy_url = manager.start_task()

    assert proxy_url.startswith("http://127.0.0.1:")
    assert manager.controller_port != manager.mixed_port
    assert manager.config_path is not None and manager.config_path.exists()


def test_runtime_selects_next_node_after_health_failure(tmp_path):
    failures: list[int] = []
    manager, controller = build_manager(
        tmp_path,
        health_results=[False, True],
        callback=lambda record, ok: failures.append(record.id) if not ok else None,
    )
    manager.start_task()

    selected = manager.select_for_cycle()

    assert selected.name == "second"
    assert failures == [1]
    assert controller.selected == ["first", "second"]


def test_runtime_release_is_idempotent_and_removes_private_files(tmp_path):
    manager, _controller = build_manager(tmp_path)
    manager.start_task()
    config_path = manager.config_path

    manager.release()
    manager.release()

    assert config_path is not None and not config_path.exists()
    assert manager.process is None


def test_runtime_reaps_process_after_kill_path(tmp_path):
    class KillRequiredProcess(FakeProcess):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            self.wait_calls = 0
            self.killed = False

        def terminate(self):
            raise TimeoutError("terminate unavailable")

        def wait(self, timeout=None):
            self.wait_calls += 1
            if not self.killed:
                raise TimeoutError("still running")
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.killed = True

    process_holder = {}

    def process_factory(*args, **kwargs):
        process = KillRequiredProcess(*args, **kwargs)
        process_holder["process"] = process
        return process

    manager, _controller = build_manager(tmp_path, process_factory=process_factory)
    manager.start_task()
    manager.release()

    process = process_holder["process"]
    assert process.killed is True
    assert process.wait_calls == 2


def test_runtime_setup_failure_removes_private_work_directory(tmp_path, monkeypatch):
    manager, _controller = build_manager(tmp_path)

    def fail_render(*_args, **_kwargs):
        raise ValueError("invalid runtime config")

    monkeypatch.setattr("core.openvpn_runtime.render_single_openvpn_config", fail_render)

    with pytest.raises(ValueError, match="invalid runtime config"):
        manager.start_task()

    assert list(tmp_path.iterdir()) == []
    assert manager.process is None
    assert manager.config_path is None


def test_runtime_does_not_return_stale_url_after_process_exit(tmp_path):
    manager, _controller = build_manager(tmp_path)
    manager.process = FakeProcess()
    manager.process.returncode = 1
    manager.proxy_url = "http://127.0.0.1:38123"

    with pytest.raises(RuntimeError, match="process exited"):
        manager.start_task()

    assert manager.process is None
    assert manager.proxy_url == ""


def test_runtime_applies_default_five_node_candidate_cap(tmp_path):
    profiles = [runtime_record(index, f"node-{index}") for index in range(1, 7)]
    manager, controller = build_manager(
        tmp_path,
        profiles=profiles,
        health_results=[False, False, False, False, False, True],
    )
    manager.start_task()

    with pytest.raises(RuntimeError, match="all OpenVPN profiles failed"):
        manager.select_for_cycle()

    assert controller.selected == [f"node-{index}" for index in range(1, 6)]
    manager.release()


def test_runtime_zero_candidate_limit_traverses_all_profiles(tmp_path):
    profiles = [runtime_record(index, f"node-{index}") for index in range(1, 7)]
    manager, controller = build_manager(
        tmp_path,
        profiles=profiles,
        health_results=[False, False, False, False, False, True],
        max_candidates=0,
    )
    manager.start_task()

    selected = manager.select_for_cycle()

    assert selected.id == 6
    assert controller.selected == [f"node-{index}" for index in range(1, 7)]


def test_runtime_rejects_negative_candidate_limit(tmp_path):
    with pytest.raises(ValueError, match="max_candidates"):
        build_manager(tmp_path, max_candidates=-1)


def test_runtime_excludes_failed_profile_ids_from_later_selection(tmp_path):
    profiles = [runtime_record(index, f"node-{index}") for index in range(1, 4)]
    manager, controller = build_manager(
        tmp_path,
        profiles=profiles,
        health_results=[False, True, True],
    )
    manager.start_task()

    first = manager.select_for_cycle()
    manager.report_cycle(False)
    second = manager.select_for_cycle()

    assert first.id == 2
    assert second.id == 3
    assert manager.failed_ids == [1, 2]
    assert controller.selected == ["node-1", "node-2", "node-3"]


def test_runtime_aborts_without_marking_node_when_mihomo_dies_during_health_check(tmp_path):
    reported = []
    manager, _controller = build_manager(
        tmp_path,
        callback=lambda profile, success: reported.append((profile.id, success)),
    )

    def health_checker(_proxy_url):
        manager.process.returncode = 1
        raise OSError("proxy connection lost")

    manager.health_checker = health_checker
    manager.start_task()

    with pytest.raises(RuntimeError, match="Mihomo exited"):
        manager.select_for_cycle()

    assert manager.failed_ids == []
    assert reported == []
    manager.release()


def test_runtime_aborts_when_process_dies_after_controller_selection(tmp_path):
    holder = {}
    health_called = []
    reported = []

    class DiesAfterSelectController(FakeController):
        def select(self, group_name: str, proxy_name: str):
            super().select(group_name, proxy_name)
            holder["manager"].process.returncode = 1

    manager, _controller = build_manager(
        tmp_path,
        controller=DiesAfterSelectController(),
        callback=lambda profile, success: reported.append((profile.id, success)),
    )
    holder["manager"] = manager
    manager.health_checker = lambda _proxy_url: health_called.append(True) or True
    manager.start_task()

    with pytest.raises(RuntimeError, match="Mihomo exited"):
        manager.select_for_cycle()

    assert health_called == []
    assert manager.failed_ids == []
    assert reported == []
    assert manager._selected is None
    manager.release()


def test_runtime_aborts_when_process_dies_after_successful_health_check(tmp_path):
    reported = []
    manager, _controller = build_manager(
        tmp_path,
        callback=lambda profile, success: reported.append((profile.id, success)),
    )

    def health_checker(_proxy_url):
        manager.process.returncode = 1
        return True

    manager.health_checker = health_checker
    manager.start_task()

    with pytest.raises(RuntimeError, match="Mihomo exited"):
        manager.select_for_cycle()

    assert manager.failed_ids == []
    assert reported == []
    assert manager._selected is None
    manager.release()


def test_runtime_default_mixed_port_probe_uses_remaining_subsecond_timeout(tmp_path, monkeypatch):
    timeouts = []

    def unavailable_connection(_address, timeout):
        timeouts.append(timeout)
        raise OSError("not ready")

    monkeypatch.setattr("core.openvpn_runtime.socket.create_connection", unavailable_connection)
    manager = MihomoRuntimeManager(
        profiles=[runtime_record(1, "first")],
        binary="mihomo",
        work_root=tmp_path,
        process_factory=FakeProcess,
        controller_factory=lambda *_args, **_kwargs: FakeController(),
        health_checker=lambda _proxy_url: True,
        start_timeout=0.05,
        randomizer=lambda items: items,
    )

    with pytest.raises(RuntimeError, match="mixed port did not become ready"):
        manager.start_task()

    assert timeouts
    assert max(timeouts) <= 0.05


def test_runtime_waits_for_local_mixed_port_after_controller_ready(tmp_path):
    events = []
    readiness = iter([False, True])

    class OrderedController(FakeController):
        def wait_ready(self, timeout):
            events.append("controller")
            return True

    def mixed_port_checker(port, _timeout):
        events.append(("mixed", port))
        return next(readiness)

    manager, _controller = build_manager(
        tmp_path,
        controller=OrderedController(),
        mixed_port_checker=mixed_port_checker,
        start_timeout=0.2,
    )

    manager.start_task()

    assert events[0] == "controller"
    assert [event[0] for event in events[1:]] == ["mixed", "mixed"]
    assert all(event[1] == manager.mixed_port for event in events[1:])


def test_runtime_keeps_controller_and_mixed_ports_distinct(tmp_path, monkeypatch):
    ports = iter([30101, 30101, 30102])
    monkeypatch.setattr(MihomoRuntimeManager, "_free_port", staticmethod(lambda: next(ports)))
    manager, _controller = build_manager(tmp_path)

    manager.start_task()

    assert manager.mixed_port == 30101
    assert manager.controller_port == 30102


def test_runtime_statistics_callback_errors_are_best_effort(tmp_path):
    def broken_callback(_profile, _success):
        raise RuntimeError("statistics unavailable")

    manager, _controller = build_manager(
        tmp_path,
        health_results=[False, True],
        callback=broken_callback,
    )
    manager.start_task()

    selected = manager.select_for_cycle()
    manager.report_cycle(True)

    assert selected.id == 2
    assert manager.failed_ids == [1]


def test_runtime_release_cleans_up_when_controller_close_fails(tmp_path):
    class CloseFailController(FakeController):
        def close(self):
            raise RuntimeError("controller close failed")

    manager, _controller = build_manager(tmp_path, controller=CloseFailController())
    manager.start_task()
    process = manager.process
    work_dir = manager._work_dir

    manager.release()

    assert process is not None and process.terminated is True
    assert work_dir is not None and not work_dir.exists()
    assert manager.process is None
    manager.release()


def test_openvpn_task_session_release_retries_after_runtime_failure():
    class FlakyRuntime:
        def __init__(self):
            self.calls = 0

        def release(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("release failed")

    runtime = FlakyRuntime()
    session = OpenVPNTaskSession(runtime)

    with pytest.raises(RuntimeError, match="release failed"):
        session.release()
    session.release()

    assert runtime.calls == 2


def test_openvpn_task_session_rejects_start_after_release():
    class Runtime:
        def __init__(self):
            self.start_calls = 0
            self.release_calls = 0

        def start_task(self):
            self.start_calls += 1
            return "http://127.0.0.1:38123"

        def release(self):
            self.release_calls += 1

    runtime = Runtime()
    session = OpenVPNTaskSession(runtime)
    session.release()

    with pytest.raises(RuntimeError, match="session is not active"):
        session.start()

    assert runtime.start_calls == 0
    assert runtime.release_calls == 1


def test_runtime_probe_all_checks_every_profile_without_reporting(tmp_path):
    reported = []
    profiles = [runtime_record(index, f"node-{index}") for index in range(1, 4)]
    manager, controller = build_manager(
        tmp_path,
        profiles=profiles,
        health_results=[False, True, False],
        callback=lambda profile, success: reported.append((profile.id, success)),
        max_candidates=1,
    )
    manager.start_task()

    results = manager.probe_all()

    assert [(result.profile.id, result.healthy) for result in results] == [
        (1, False),
        (2, True),
        (3, False),
    ]
    assert controller.selected == ["node-1", "node-2", "node-3"]
    assert reported == []
    manager.release()


def test_runtime_probe_all_aborts_when_process_exits(tmp_path):
    profiles = [runtime_record(index, f"node-{index}") for index in range(1, 4)]
    manager, controller = build_manager(
        tmp_path,
        profiles=profiles,
        health_results=[True, True, True],
    )

    def health_checker(_proxy_url):
        manager.process.returncode = 1
        return True

    manager.health_checker = health_checker
    manager.start_task()

    with pytest.raises(RuntimeError, match="Mihomo exited"):
        manager.probe_all()

    assert controller.selected == ["node-1"]
    manager.release()
