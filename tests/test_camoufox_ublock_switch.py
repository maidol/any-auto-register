"""CAMOUFOX_UBLOCK 开关：设为 false 时，Camoufox 不再装它默认自带的 uBlock Origin。

作用范围是 ChatGPT 注册的两个浏览器入口（run() 和 _retry_oauth_fresh_browser()）
和 Temp-Mail Web 邮箱的浏览器。本地 Turnstile Solver 不受这个开关影响。
"""
import pytest
from camoufox.addons import DefaultAddons

import camoufox.sync_api
import core.base_mailbox as bm
import platforms.chatgpt.browser_register as br
from core.camoufox_addons import apply_ublock_switch


class _Sentinel(BaseException):
    """从假 Camoufox 里逃出来。必须继承 BaseException：
    _retry_oauth_fresh_browser 外面有 except Exception，会把 Exception 吞掉。"""


@pytest.fixture
def launches(monkeypatch):
    calls = []

    def _fake_camoufox(**kwargs):
        calls.append(dict(kwargs))
        raise _Sentinel()

    monkeypatch.setattr(br, "Camoufox", _fake_camoufox)
    monkeypatch.setattr(camoufox.sync_api, "Camoufox", _fake_camoufox)
    return calls


def _set(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CAMOUFOX_UBLOCK", raising=False)
    else:
        monkeypatch.setenv("CAMOUFOX_UBLOCK", value)


@pytest.mark.parametrize(
    "value", [None, "", "true", "1", "yes", "on", "TRUE", "whatever"]
)
def test_ublock_kept_when_unset_or_not_an_off_value(monkeypatch, value):
    _set(monkeypatch, value)
    opts = {"headless": True}
    assert apply_ublock_switch(opts) is opts
    assert opts == {"headless": True}


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " False ", "OFF"])
def test_ublock_excluded_for_off_values(monkeypatch, value):
    _set(monkeypatch, value)
    opts = {"headless": True}
    assert apply_ublock_switch(opts) is opts
    assert opts == {"headless": True, "exclude_addons": [DefaultAddons.UBO]}


def _register():
    return br.ChatGPTBrowserRegister(headless=True, log_fn=lambda *_a, **_k: None)


def _run(reg):
    reg.run(email="a@example.com", password="Pw123456!x")


def _oauth_retry(reg):
    reg._retry_oauth_fresh_browser("a@example.com", "Pw123456!x")


@pytest.mark.parametrize("entry", [_run, _oauth_retry], ids=["run", "oauth_retry"])
def test_chatgpt_browser_entries_follow_switch(monkeypatch, launches, entry):
    _set(monkeypatch, "false")
    with pytest.raises(_Sentinel):
        entry(_register())
    _set(monkeypatch, None)
    with pytest.raises(_Sentinel):
        entry(_register())
    assert len(launches) == 2
    assert launches[0].get("exclude_addons") == [DefaultAddons.UBO]
    assert "exclude_addons" not in launches[1]


def test_tempmail_web_browser_follows_switch(monkeypatch, launches):
    _set(monkeypatch, "false")
    with pytest.raises(_Sentinel):
        bm.TempMailWebMailbox()._ensure_browser()
    _set(monkeypatch, None)
    with pytest.raises(_Sentinel):
        bm.TempMailWebMailbox()._ensure_browser()
    assert len(launches) == 2
    assert launches[0].get("exclude_addons") == [DefaultAddons.UBO]
    assert "exclude_addons" not in launches[1]
