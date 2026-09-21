"""需求 4 守卫：每一轮注册必须开一个不带持久化 profile 的全新浏览器。

今天这条性质是**免费得来的**：`ChatGPTBrowserRegister.run()` 里的
`Camoufox(**launch_opts)` 根本没传 `user_data_dir`，所以 Camoufox 每次
自己开一个临时 profile，退出即销毁，cookie / cache 天然不跨轮残留。

问题在于**没有任何东西保证它继续成立**。`platforms.chatgpt.browser_oauth.py`
那条路径就明确接受 `chrome_user_data_dir` 并复用登录态；哪天有人为了
「少扫一次码」把同样的参数接到注册路径上，cookie 就会跨轮带过去，
而现有 200 多条测试对此**一条都不会红**。

这个文件把那条性质钉成断言。它不修任何 bug —— 它拦的是未来那次静默改动。
"""
from __future__ import annotations

import pytest

import platforms.chatgpt.browser_register as br

#: 任何一个出现在 launch_opts 里都意味着浏览器状态会跨轮存活。
PERSISTENCE_KEYS = ("user_data_dir", "persistent_context", "profile", "storage_state")


class _Sentinel(BaseException):
    """从假 Camoufox 里逃出来，不要真的去开浏览器。

    **必须继承 BaseException，不能是 Exception。** 生产代码里
    `_retry_oauth_fresh_browser` 的 `with Camoufox(...)` 外面包着
    `try: ... except Exception:`，继承 Exception 的哨兵会被它当成
    「OAuth 失败」静默吞掉，于是 `pytest.raises` 报
    `DID NOT RAISE` —— 测试从「红」退化成「看起来这条路没走到」。
    """


@pytest.fixture
def recorded_launches(monkeypatch):
    """替换 Camoufox，记录每次构造的 kwargs，然后立刻中断。"""
    calls: list[dict] = []

    def _fake_camoufox(**kwargs):
        calls.append(dict(kwargs))
        raise _Sentinel()

    monkeypatch.setattr(br, "Camoufox", _fake_camoufox)
    return calls


def _make_register():
    return br.ChatGPTBrowserRegister(headless=True, log_fn=lambda *_a, **_k: None)


def test_registration_browser_carries_no_persistent_profile(recorded_launches):
    with pytest.raises(_Sentinel):
        _make_register().run(email="a@example.com", password="Pw123456!x")

    assert len(recorded_launches) == 1, "run() 应当恰好构造一次 Camoufox"
    opts = recorded_launches[0]
    leaked = [k for k in PERSISTENCE_KEYS if k in opts]
    assert not leaked, (
        f"注册浏览器被传了持久化参数 {leaked}，cookie/cache 会跨轮残留，"
        f"违反「每一轮无痕开始」。实际 launch_opts={opts}"
    )


def test_oauth_retry_browser_carries_no_persistent_profile(recorded_launches):
    """`_retry_oauth_fresh_browser` 的名字里就写着 fresh，把它钉住。"""
    with pytest.raises(_Sentinel):
        _make_register()._retry_oauth_fresh_browser("a@example.com", "Pw123456!x")

    assert len(recorded_launches) == 1
    opts = recorded_launches[0]
    leaked = [k for k in PERSISTENCE_KEYS if k in opts]
    assert not leaked, f"OAuth 重试浏览器被传了持久化参数 {leaked}，实际 launch_opts={opts}"


def test_each_cycle_constructs_a_brand_new_browser(recorded_launches):
    """两轮注册必须是两次独立构造，不能复用同一个上下文对象。"""
    reg = _make_register()
    for _ in range(2):
        with pytest.raises(_Sentinel):
            reg.run(email="a@example.com", password="Pw123456!x")

    assert len(recorded_launches) == 2, (
        "两轮注册只构造了 "
        f"{len(recorded_launches)} 次浏览器 —— 上下文被复用了，cookie 会跨轮带过去"
    )


def test_proxy_does_not_smuggle_in_a_profile(recorded_launches):
    """带代理那条分支会多塞 geoip，确认它没顺手带进持久化参数。"""
    reg = br.ChatGPTBrowserRegister(
        headless=True, proxy="http://u:p@127.0.0.1:8080", log_fn=lambda *_a, **_k: None
    )
    with pytest.raises(_Sentinel):
        reg.run(email="a@example.com", password="Pw123456!x")

    opts = recorded_launches[0]
    assert opts.get("geoip") is True, "带代理时应当开 geoip"
    leaked = [k for k in PERSISTENCE_KEYS if k in opts]
    assert not leaked, f"带代理的分支漏了持久化参数 {leaked}"
