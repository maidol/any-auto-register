"""SOLVER_AUTOSTART 开关：只管开机自启（main.py lifespan 里的 start_async）。

按需启动入口 start()（core/base_platform.py、platforms/tavily/plugin.py 调用）
和「重启 Solver」接口不受这个开关影响，所以这里只测 start_async。
"""
import pytest

import services.solver_manager as sm


class _SyncThread:
    """代替 threading.Thread：start() 时同步执行 target，测试不靠线程调度时机。"""

    created = []

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon
        _SyncThread.created.append(self)

    def start(self):
        self.target()


@pytest.fixture
def calls(monkeypatch):
    seen = []
    _SyncThread.created = []
    monkeypatch.setattr(sm, "start", lambda: seen.append("start"))
    monkeypatch.setattr(sm.threading, "Thread", _SyncThread)
    return seen


def _start_async_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SOLVER_AUTOSTART", raising=False)
    else:
        monkeypatch.setenv("SOLVER_AUTOSTART", value)
    return sm.start_async()


@pytest.mark.parametrize(
    "value", [None, "", "true", "1", "yes", "on", "TRUE", "whatever"]
)
def test_autostart_on_when_unset_or_not_an_off_value(monkeypatch, calls, value):
    t = _start_async_with(monkeypatch, value)
    assert calls == ["start"]
    assert len(_SyncThread.created) == 1
    assert _SyncThread.created[0].daemon is True
    assert t is _SyncThread.created[0]


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " False ", "OFF"])
def test_autostart_off_values_skip_start(monkeypatch, calls, value):
    t = _start_async_with(monkeypatch, value)
    assert calls == []
    assert _SyncThread.created == []
    assert t is None
