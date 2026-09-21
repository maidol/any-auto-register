"""`PinnedMailbox` 与 `close()` 这一族的单元行为。

上面那个文件（`test_cycle_identity_and_cleanup.py`）钉的是任务级现象，
这里钉的是承载它的两个零件本身，因为这两个零件各有一个**不会报错**的写错法：

* `PinnedMailbox` 把 `get_current_ids()` 跟 `get_email()` 一起缓存 ——
  看起来更「一致」，实际让 before_ids 地板冻在周期开始那一刻；
* `FallbackMailbox.close()` 让第一个 provider 的异常冒出去 ——
  后面几个 provider 一个都没关，而调用方的 try/except 会把它记成一条警告。
"""
from __future__ import annotations

import pytest

from core.base_mailbox import (
    DEFAULT_TEMPMAIL_WEB_BASE_URL,
    BaseMailbox,
    FallbackMailbox,
    MailboxAccount,
    PinnedMailbox,
    TempMailWebMailbox,
)


class _CountingMailbox(BaseMailbox):
    def __init__(self) -> None:
        self.get_email_calls = 0
        self.ids: set = set()
        self.wait_calls: list[dict] = []
        self.close_calls = 0
        self.close_raises = False

    def get_email(self) -> MailboxAccount:
        self.get_email_calls += 1
        return MailboxAccount(email=f"box{self.get_email_calls}@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set(self.ids)

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                      code_pattern=None) -> str:
        self.wait_calls.append({"email": account.email, "before_ids": set(before_ids or ())})
        return "123456"

    def wait_for_link(self, account, keyword="", timeout=120, before_ids=None) -> str:
        return "https://example.com/verify"

    def close(self) -> None:
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError(f"{id(self)} refused to close")

    # provider 特有方法：必须能穿过包装层
    def provider_specific(self) -> str:
        return "ok"


# --------------------------------------------------------------------------
# PinnedMailbox
# --------------------------------------------------------------------------

def test_get_email_is_resolved_once_and_then_pinned():
    inner = _CountingMailbox()
    pinned = PinnedMailbox(inner)

    first = pinned.get_email()
    again = pinned.get_email()

    assert inner.get_email_calls == 1, f"底层被要了 {inner.get_email_calls} 次邮箱"
    assert again is first


def test_get_current_ids_is_never_cached():
    """地板必须每次现取 —— 这是「读走过期验证码」那条的根。"""
    inner = _CountingMailbox()
    pinned = PinnedMailbox(inner)
    account = pinned.get_email()

    assert pinned.get_current_ids(account) == set()
    inner.ids = {"CODE-A"}
    assert pinned.get_current_ids(account) == {"CODE-A"}, (
        "第二次取到的地板还是旧的 —— get_current_ids 被连同 get_email 一起缓存了"
    )


def test_wait_for_code_passes_through_untouched():
    inner = _CountingMailbox()
    pinned = PinnedMailbox(inner)
    account = pinned.get_email()

    pinned.wait_for_code(account, keyword="k", timeout=7, before_ids={"CODE-A"})

    assert inner.wait_calls == [{"email": account.email, "before_ids": {"CODE-A"}}]


def test_unknown_attributes_reach_the_wrapped_provider():
    """平台代码会直接摸 provider 上的自定义方法，包装层不能把它们挡住。"""
    inner = _CountingMailbox()
    pinned = PinnedMailbox(inner)

    assert pinned.provider_specific() == "ok"
    assert pinned.wrapped is inner


# --------------------------------------------------------------------------
# close()
# --------------------------------------------------------------------------

def test_base_mailbox_close_is_a_no_op_by_default():
    """12 个 provider 里只有 TempMailWeb 真的持有浏览器，其余不该被迫实现。"""
    inner = _CountingMailbox.__mro__  # 只是为了确认基类可被继承
    assert inner is not None

    class _Bare(BaseMailbox):
        def get_email(self):
            return MailboxAccount(email="a@example.com")

        def get_current_ids(self, account):
            return set()

        def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                          code_pattern=None):
            return ""

    _Bare().close()  # 不抛就是通过


def test_fallback_close_reaches_every_provider_even_if_one_raises():
    """第一个 provider 炸了，后面的照样要关 —— 否则是一次静默泄漏。"""
    a, b, c = _CountingMailbox(), _CountingMailbox(), _CountingMailbox()
    b.close_raises = True
    fallback = FallbackMailbox([("a", a), ("b", b), ("c", c)])

    fallback.close()

    assert (a.close_calls, b.close_calls, c.close_calls) == (1, 1, 1), (
        f"只关到了 {(a.close_calls, b.close_calls, c.close_calls)} —— "
        "中间那个抛异常之后就没往下走了"
    )


class _FakeFuture:
    def __init__(self, value) -> None:
        self._value = value

    def result(self, timeout=None):
        return self._value


class _FakeExecutor:
    def __init__(self) -> None:
        self.submitted = 0
        self.shutdown_calls = 0

    def submit(self, fn, *args, **kwargs):
        self.submitted += 1
        return _FakeFuture(fn(*args, **kwargs))

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_calls += 1


class _FakeBrowser:
    def __init__(self) -> None:
        self.exits = 0

    def __exit__(self, *_args):
        self.exits += 1


def test_tempmail_web_close_releases_the_browser_and_the_executor():
    mailbox = TempMailWebMailbox(base_url=DEFAULT_TEMPMAIL_WEB_BASE_URL)
    executor, browser = _FakeExecutor(), _FakeBrowser()
    mailbox._executor = executor
    mailbox._browser = browser
    mailbox._page = object()

    mailbox.close()

    assert browser.exits == 1, "浏览器没有退出，cookie / cache 会带进下一轮"
    assert executor.shutdown_calls == 1, "浏览器线程没有关"
    assert mailbox._page is None and mailbox._browser is None and mailbox._executor is None, (
        "句柄没清空 —— 下一轮 _ensure_browser() 会直接复用上一轮那个页面"
    )


def test_tempmail_web_close_is_idempotent():
    """周期清理会被调多次（最后一轮 + 任务收尾），第二次不许炸。"""
    mailbox = TempMailWebMailbox(base_url=DEFAULT_TEMPMAIL_WEB_BASE_URL)
    executor, browser = _FakeExecutor(), _FakeBrowser()
    mailbox._executor = executor
    mailbox._browser = browser

    mailbox.close()
    mailbox.close()

    assert browser.exits == 1
    assert executor.shutdown_calls == 1
