"""Task-scoped Mihomo runtime for OpenVPN proxy records."""

from __future__ import annotations

import hashlib
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable

import requests

from core.openvpn_config import render_single_openvpn_config
from domain.openvpn_proxies import (
    OpenVPNProbeResult,
    OpenVPNProxyRuntimeRecord,
    openvpn_proxy_identity,
)


class MihomoRuntimeError(RuntimeError):
    pass


class _HttpController:
    def __init__(self, address: str, secret: str):
        self.base_url = f"http://{address}"
        self.headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        self.session = requests.Session()
        self.session.trust_env = False

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self.session.get(
                    f"{self.base_url}/version",
                    headers=self.headers,
                    timeout=min(1.0, max(deadline - time.monotonic(), 0.1)),
                )
                if response.ok:
                    return True
            except requests.RequestException:
                pass
            time.sleep(0.1)
        return False

    def select(self, group_name: str, proxy_name: str) -> None:
        response = self.session.put(
            f"{self.base_url}/proxies/{group_name}",
            headers=self.headers,
            json={"name": proxy_name},
            timeout=5,
        )
        if not response.ok:
            raise MihomoRuntimeError(
                f"Mihomo rejected node selection: HTTP {response.status_code}"
            )

    def close(self) -> None:
        self.session.close()


class MihomoRuntimeManager:
    def __init__(
        self,
        profiles: Iterable[OpenVPNProxyRuntimeRecord],
        *,
        binary: str,
        work_root: str | Path,
        start_timeout: float = 20,
        health_url: str = "https://www.gstatic.com/generate_204",
        health_timeout: float = 20,
        max_candidates: int | None = 5,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        controller_factory: Callable[..., object] | None = None,
        health_checker: Callable[[str], bool] | None = None,
        mixed_port_checker: Callable[[int, float], bool] | None = None,
        report_callback: Callable[[OpenVPNProxyRuntimeRecord, bool], None] | None = None,
        randomizer: Callable[[list[OpenVPNProxyRuntimeRecord]], None] | None = None,
    ):
        self.profiles = list(profiles)
        self.binary = binary
        self.work_root = Path(work_root)
        self.start_timeout = float(start_timeout)
        self.health_url = health_url
        self.health_timeout = float(health_timeout)
        if max_candidates is None:
            self.max_candidates = None
        else:
            max_candidates = int(max_candidates)
            if max_candidates < 0:
                raise ValueError("max_candidates must be zero or a positive integer")
            self.max_candidates = None if max_candidates == 0 else max_candidates
        self.process_factory = process_factory
        self.controller_factory = controller_factory or (lambda address, secret: _HttpController(address, secret))
        self.health_checker = health_checker or self._default_health_checker
        self.mixed_port_checker = mixed_port_checker or self._default_mixed_port_checker
        self.report_callback = report_callback
        self.randomizer = randomizer or secrets.SystemRandom().shuffle
        self.process: subprocess.Popen | None = None
        self.controller: object | None = None
        self.proxy_url = ""
        self.mixed_port = 0
        self.controller_port = 0
        self.controller_secret = ""
        self.config_path: Path | None = None
        self._work_dir: Path | None = None
        self._selected: OpenVPNProxyRuntimeRecord | None = None
        self._reported = False
        self.failed_ids: list[int] = []
        self._runtime_names: dict[int, str] = {}

    def _runtime_config_entries(self) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        used_names: set[str] = set()
        self._runtime_names.clear()
        for profile in self.profiles:
            config = dict(profile.config or {})
            name = str(config.get("name") or profile.name)
            runtime_name = name
            if runtime_name.casefold() in used_names:
                identity = openvpn_proxy_identity(profile.name, profile.server, profile.port)
                suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
                runtime_name = f"{name[:180]}-{suffix}"
                counter = 1
                while runtime_name.casefold() in used_names:
                    runtime_name = f"{name[:175]}-{suffix}-{counter}"
                    counter += 1
            used_names.add(runtime_name.casefold())
            self._runtime_names[profile.id] = runtime_name
            config["name"] = runtime_name
            entries.append(config)
        return entries

    def start_task(self) -> str:
        if self.process is not None:
            if self.process.poll() is None:
                return self.proxy_url
            self.release()
            raise MihomoRuntimeError("Mihomo process exited before start_task completed")
        if not self.profiles:
            raise MihomoRuntimeError("no active OpenVPN profiles")
        self.failed_ids.clear()
        try:
            self._work_dir = Path(tempfile.mkdtemp(prefix="mihomo-", dir=self.work_root))
            self._work_dir.chmod(0o700)
            self.mixed_port = self._free_port()
            self.controller_port = self._free_port()
            while self.controller_port == self.mixed_port:
                self.controller_port = self._free_port()
            self.controller_secret = secrets.token_urlsafe(24)
            controller_address = f"127.0.0.1:{self.controller_port}"
            config = render_single_openvpn_config(
                self._runtime_config_entries(),
                mixed_port=self.mixed_port,
                controller=controller_address,
            )
            config = config + f"secret: {self.controller_secret!r}\n"
            self.config_path = self._work_dir / "config.yaml"
            self.config_path.write_text(config, encoding="utf-8")
            self.config_path.chmod(0o600)
            self.process = self.process_factory(
                [self.binary, "-f", str(self.config_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
            )
            self.controller = self.controller_factory(controller_address, self.controller_secret)
            wait_ready = getattr(self.controller, "wait_ready", None)
            if callable(wait_ready) and not wait_ready(self.start_timeout):
                raise MihomoRuntimeError("Mihomo controller did not become ready")
            if not self._wait_for_mixed_port(self.start_timeout):
                raise MihomoRuntimeError("Mihomo mixed port did not become ready")
            self.proxy_url = f"http://127.0.0.1:{self.mixed_port}"
            return self.proxy_url
        except Exception:
            self.release()
            raise

    def select_for_cycle(self) -> OpenVPNProxyRuntimeRecord:
        if self.process is None or self.controller is None:
            raise MihomoRuntimeError("Mihomo runtime is not started")
        candidates = [profile for profile in self.profiles if profile.id not in self.failed_ids]
        self.randomizer(candidates)
        if self.max_candidates is not None:
            candidates = candidates[: self.max_candidates]
        self._selected = None
        self._reported = False
        for profile in candidates:
            if self.process.poll() is not None:
                raise MihomoRuntimeError("Mihomo exited while selecting an OpenVPN node")
            try:
                runtime_name = self._runtime_names.get(profile.id, profile.name)
                self.controller.select("VPNGate", runtime_name)
                if self.process.poll() is not None:
                    raise MihomoRuntimeError("Mihomo exited while selecting an OpenVPN node")
                if not self.health_checker(self.proxy_url):
                    raise MihomoRuntimeError("OpenVPN proxy health check failed")
                if self.process.poll() is not None:
                    raise MihomoRuntimeError("Mihomo exited while selecting an OpenVPN node")
            except Exception as exc:
                if self.process.poll() is not None:
                    raise MihomoRuntimeError("Mihomo exited while selecting an OpenVPN node") from exc
                self._mark_failed(profile)
                self._report(profile, False)
                continue
            self._selected = profile
            return profile
        raise MihomoRuntimeError("all OpenVPN profiles failed health checks")

    def probe_all(self) -> list[OpenVPNProbeResult]:
        """Health-check every profile without changing task statistics."""
        if self.process is None or self.controller is None:
            raise MihomoRuntimeError("Mihomo runtime is not started")

        results: list[OpenVPNProbeResult] = []
        for profile in self.profiles:
            if self.process.poll() is not None:
                raise MihomoRuntimeError("Mihomo exited while probing OpenVPN nodes")
            runtime_name = self._runtime_names.get(profile.id, profile.name)
            try:
                self.controller.select("VPNGate", runtime_name)
            except Exception as exc:
                if self.process.poll() is not None:
                    raise MihomoRuntimeError("Mihomo exited while probing OpenVPN nodes") from exc
                results.append(
                    OpenVPNProbeResult(
                        profile=profile,
                        healthy=False,
                        reason="controller-selection-failed",
                    )
                )
                continue

            if self.process.poll() is not None:
                raise MihomoRuntimeError("Mihomo exited while probing OpenVPN nodes")
            try:
                healthy = bool(self.health_checker(self.proxy_url))
            except Exception:
                healthy = False
            if self.process.poll() is not None:
                raise MihomoRuntimeError("Mihomo exited while probing OpenVPN nodes")
            results.append(
                OpenVPNProbeResult(
                    profile=profile,
                    healthy=healthy,
                    reason="" if healthy else "proxy-health-failed",
                )
            )
        return results

    def report_cycle(self, success: bool) -> None:
        if self._selected is None or self._reported:
            return
        selected = self._selected
        self._reported = True
        if not success:
            self._mark_failed(selected)
        self._report(selected, success)

    def release(self) -> None:
        process, work_dir, controller = self.process, self._work_dir, self.controller
        self.process = None
        self.controller = None
        self._selected = None
        self.proxy_url = ""
        self.config_path = None
        self._work_dir = None
        self.mixed_port = 0
        self.controller_port = 0
        self.controller_secret = ""
        try:
            close_controller = getattr(controller, "close", None)
            if callable(close_controller):
                close_controller()
        except Exception:
            pass
        try:
            if process is not None and process.poll() is None:
                process.terminate()
        except Exception:
            pass
        try:
            if process is not None and process.poll() is None:
                process.wait(timeout=5)
        except Exception:
            pass
        killed = False
        try:
            if process is not None and process.poll() is None:
                process.kill()
                killed = True
        except Exception:
            pass
        if killed and process is not None:
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _mark_failed(self, profile: OpenVPNProxyRuntimeRecord) -> None:
        if profile.id not in self.failed_ids:
            self.failed_ids.append(profile.id)

    def _report(self, profile: OpenVPNProxyRuntimeRecord, success: bool) -> None:
        if self.report_callback is not None:
            try:
                self.report_callback(profile, success)
            except Exception:
                pass

    def _wait_for_mixed_port(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                if self.mixed_port_checker(self.mixed_port, remaining):
                    return True
            except OSError:
                pass
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        return False

    @staticmethod
    def _default_mixed_port_checker(port: int, timeout: float) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=max(float(timeout), 0.001)):
                return True
        except OSError:
            return False

    def _default_health_checker(self, proxy_url: str) -> bool:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                self.health_url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=self.health_timeout,
            )
            return 200 <= response.status_code < 400
        finally:
            session.close()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
