from __future__ import annotations

from typing import Any


class OpenVPNTaskSession:
    """Owns one task-scoped Mihomo runtime and its cycle selection boundary."""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self._started = False
        self._released = False

    def start(self) -> str:
        if self._released:
            raise RuntimeError("OpenVPN task session is not active")
        try:
            proxy_url = self.runtime.start_task()
            self._started = True
            return proxy_url
        except Exception:
            self.release()
            raise

    def select_for_cycle(self):
        if not self._started or self._released:
            raise RuntimeError("OpenVPN task session is not active")
        return self.runtime.select_for_cycle()

    def report_cycle(self, success: bool) -> None:
        if self._started and not self._released:
            self.runtime.report_cycle(success)

    def release(self) -> None:
        if self._released:
            return
        self.runtime.release()
        self._released = True
