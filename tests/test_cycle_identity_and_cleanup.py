"""需求 1d 与需求 4：一轮 = 同一个邮箱，一轮结束要清干净。

「轮」在今天的代码里**不存在实体**。`AccountCycleRunner` 知道周期，但每次
尝试都新造一个 platform 实例，`MailboxIdentityProvider.resolve()` 于是
无条件调一次 `mailbox.get_email()` —— 线上 12 个 provider 里有 10 个
每次调用都开一个新地址。所以「同一个邮箱重试 3 次」实际是「3 个邮箱各试 1 次」。

第二件事跟着来：一旦邮箱被钉住跨尝试复用，`before_ids` 这个地板就必须
**每次尝试重新取**。第 1 次尝试收到的验证码会留在收件箱里，用周期开始那一刻
的地板去 `wait_for_code`，那封过期邮件就是「新邮件」—— 平台拿到一个早已作废的
验证码，报的是「验证码错误」而不是「超时」。**这个现象会把人引向完全错误的方向。**

这些测试把平台、落库、代理池换成假的，只留身份与生命周期语义本身。
"""
from __future__ import annotations

import pytest

from application.tasks import (
    TASK_STATUS_SUCCEEDED,
    TaskLogger,
    _execute_register_task,
    create_task,
    get_task,
)
from core.base_identity import MailboxIdentityProvider
from core.base_mailbox import BaseMailbox, MailboxAccount


class FakeMailbox(BaseMailbox):
    """每次 `get_email()` 发一个新地址 —— 线上 10/12 个 provider 就是这个行为。

    `messages` 是「这个地址的收件箱」，`deliver()` 模拟一封验证码邮件到达。
    """

    def __init__(self) -> None:
        self.get_email_calls = 0
        self.issued: list[str] = []
        self.messages: dict[str, list[str]] = {}
        self.get_current_ids_calls: list[str] = []
        self.close_calls = 0
        self.close_raises = False

    def get_email(self) -> MailboxAccount:
        self.get_email_calls += 1
        addr = f"box{self.get_email_calls}@example.com"
        self.issued.append(addr)
        self.messages.setdefault(addr, [])
        return MailboxAccount(email=addr, account_id=addr)

    def get_current_ids(self, account: MailboxAccount) -> set:
        self.get_current_ids_calls.append(account.email)
        return set(self.messages.get(account.email, []))

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                      code_pattern=None) -> str:
        seen = set(before_ids or ())
        for mid in self.messages.get(account.email, []):
            if mid not in seen:
                return mid
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def deliver(self, email: str, code: str) -> None:
        self.messages.setdefault(email, []).append(code)

    def close(self) -> None:
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("close blew up")


class FakeAccount:
    def __init__(self, email: str) -> None:
        self.email = email
        self.password = "pw"
        self.platform = "chatgpt"
        self.extra: dict = {}
        self.user_id = ""
        self.token = ""


class FakePlatform:
    """走**真的** `MailboxIdentityProvider.resolve()`，只把注册结果换成脚本。

    不能在这里自己 `get_email()` —— 那样测的是假货自己的行为。身份是怎么被
    解析出来的正是本文件要钉的东西，必须过生产那段代码。
    """

    def __init__(self, world: "FakeWorld", proxy, mailbox) -> None:
        self.world = world
        self.proxy = proxy
        self.mailbox = mailbox

    def register(self, email=None, password=None):
        w = self.world
        attempt_no = len(w.identities)
        identity = MailboxIdentityProvider(mailbox=self.mailbox).resolve(email)
        w.identities.append(identity)
        w.mailboxes_seen.append(self.mailbox)

        script = w.on_attempt[attempt_no] if attempt_no < len(w.on_attempt) else None
        if callable(script):
            script(identity)

        outcome = w.outcomes[attempt_no] if attempt_no < len(w.outcomes) else w.default_outcome
        if outcome != "ok":
            raise RuntimeError(outcome)
        return FakeAccount(identity.email)

    def set_logger(self, _fn):
        return None


class FakeProxyPool:
    def get_next(self, region: str = ""):
        return None

    def report_success(self, url):
        return None

    def report_fail(self, url):
        return None


class FakeWorld:
    def __init__(self, monkeypatch, *, outcomes, default_outcome="boom") -> None:
        self.outcomes = list(outcomes)
        self.default_outcome = default_outcome
        self.on_attempt: list = []
        self.identities: list = []
        self.mailboxes_seen: list = []
        self.saved: list[FakeAccount] = []
        self.mailbox = FakeMailbox()
        self.task = create_task(task_type="register", platform="chatgpt", payload={})
        self.task_id = self.task["id"]
        self.logger = TaskLogger(self.task_id)

        def _build(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
            return FakePlatform(self, resolved_proxy, shared_mailbox)

        monkeypatch.setattr("application.tasks._build_platform_instance", _build)
        monkeypatch.setattr("application.tasks.save_account", self.saved.append)
        monkeypatch.setattr("application.tasks._save_task_log", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks._auto_followup_windsurf_payment", lambda **k: None)
        monkeypatch.setattr("application.tasks._auto_upload_cpa", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks._auto_push_any2api", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks.get", lambda _name: FakePlatform)
        monkeypatch.setattr("core.proxy_pool.proxy_pool", FakeProxyPool())
        monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **kw: self.mailbox)
        monkeypatch.setattr(
            "infrastructure.provider_settings_repository.ProviderSettingsRepository"
            ".get_default_provider_key",
            lambda self, kind: "moemail",
        )

    def run(self, **payload):
        body = {"platform": "chatgpt"}
        body.update(payload)
        _execute_register_task(body, self.logger)
        return get_task(self.task_id)

    @property
    def emails(self) -> list[str]:
        return [identity.email for identity in self.identities]


def world(monkeypatch, *, outcomes=(), default_outcome="boom"):
    return FakeWorld(monkeypatch, outcomes=outcomes, default_outcome=default_outcome)


# --------------------------------------------------------------------------
# 需求 1d —— 一轮 = 同一个邮箱
# --------------------------------------------------------------------------

def test_every_attempt_of_one_cycle_uses_the_same_email(monkeypatch):
    """今天三次重试是三个不同的邮箱，「重试」名不副实。"""
    w = world(monkeypatch, outcomes=[])
    w.run(count=1, retry_count=2, max_attempts=3)

    assert len(w.emails) == 3, f"应当跑满 3 次尝试，实际 {len(w.emails)} 次"
    assert len(set(w.emails)) == 1, (
        f"同一轮的三次尝试用了 {len(set(w.emails))} 个不同邮箱: {w.emails}；"
        "需求要求整轮（含重试）锁定同一个邮箱"
    )


def test_a_new_cycle_gets_a_fresh_email(monkeypatch):
    """两个方向一起钉：轮内必须同一个，轮间必须不同。

    今天红在第二个 assert（轮内换了邮箱）。第三个 assert 今天是绿的，
    它拦的是修过头——「把 get_email() 缓存到任务级」，那样整个任务共用一个邮箱。
    """
    w = world(monkeypatch, outcomes=["boom", "ok", "ok"])
    w.run(count=2, retry_count=1)

    assert len(w.emails) == 3, f"应当是 2+1 三次尝试，实际 {len(w.emails)}"
    assert w.emails[0] == w.emails[1], f"第一轮的重试换了邮箱: {w.emails[:2]}"
    assert w.emails[2] != w.emails[0], (
        f"第二轮复用了第一轮的邮箱 {w.emails[2]} —— 邮箱被钉到了任务级而不是周期级"
    )


def test_before_ids_is_re_snapshotted_at_the_start_of_every_attempt(monkeypatch):
    """钉住邮箱之后最贵的那个坑：地板必须每次尝试重取。

    第 1 次尝试收到验证码 CODE-A 之后失败（典型：接码那一步挂了）。
    第 2 次尝试若沿用周期开始那一刻的地板，CODE-A 就是「新邮件」，
    平台会拿一个早已作废的码去提交 —— 现象是「验证码错误」，不是「超时」。
    """
    w = world(monkeypatch, outcomes=[])

    def _first_attempt_receives_a_code(identity):
        w.mailbox.deliver(identity.email, "CODE-A")

    w.on_attempt = [_first_attempt_receives_a_code]
    w.run(count=1, retry_count=1, max_attempts=2)

    assert len(w.identities) == 2, f"应当是 2 次尝试，实际 {len(w.identities)}"
    second = w.identities[1]
    assert "CODE-A" in second.before_ids, (
        f"第 2 次尝试的 before_ids 地板是 {second.before_ids!r}，没有包含第 1 次"
        "尝试收到的 CODE-A —— wait_for_code 会把那封过期邮件当成新邮件读走"
    )


def test_a_stale_code_from_the_previous_attempt_is_not_readable(monkeypatch):
    """上一条的行为版：直接拿第 2 次尝试的地板去收信，必须收不到 CODE-A。

    这一条今天也是**绿**的（今天第 2 次尝试是个全新空邮箱，当然读不到）。
    它拦的是「把 get_current_ids 和 get_email 一起缓存」那种修法 ——
    那种修法下上面那条和这一条会**一起**变红。
    """
    w = world(monkeypatch, outcomes=[])

    def _first_attempt_receives_a_code(identity):
        w.mailbox.deliver(identity.email, "CODE-A")

    w.on_attempt = [_first_attempt_receives_a_code]
    w.run(count=1, retry_count=1, max_attempts=2)

    second = w.identities[1]
    with pytest.raises(TimeoutError):
        w.mailbox.wait_for_code(
            second.mailbox_account, timeout=1, before_ids=second.before_ids
        )


# --------------------------------------------------------------------------
# 需求 4 —— 每轮结束清理干净
# --------------------------------------------------------------------------

def test_mailbox_is_closed_once_per_cycle_including_the_last_one(monkeypatch):
    """两轮就要关两次。最后一轮**也**要关 —— 「下一轮开始前再关」会漏掉它。"""
    w = world(monkeypatch, outcomes=["boom", "ok", "ok"])
    task = w.run(count=2, retry_count=1)

    assert task["status"] == TASK_STATUS_SUCCEEDED
    assert w.mailbox.close_calls == 2, (
        f"两轮注册只调了 {w.mailbox.close_calls} 次 close() —— "
        "上一轮的 provider 侧浏览器（cookie、cache）带进了下一轮"
    )


def test_close_is_not_called_between_attempts_of_one_cycle(monkeypatch):
    """边界是**周期**不是尝试：轮内重试要接着用同一个邮箱，中途关掉就自相矛盾。"""
    w = world(monkeypatch, outcomes=[])
    w.run(count=1, retry_count=2, max_attempts=3)

    assert len(w.identities) == 3
    assert w.mailbox.close_calls == 1, (
        f"一轮三次尝试调了 {w.mailbox.close_calls} 次 close() —— "
        "清理挂到了每次尝试上，而不是每轮结束"
    )


def test_clean_browser_context_false_skips_the_teardown(monkeypatch):
    """逃生开关。今天是**绿**的（今天压根不关），它拦的是「把开关写死成 True」。"""
    w = world(monkeypatch, outcomes=["boom", "ok", "ok"])
    w.run(count=2, retry_count=1, clean_browser_context=False)

    assert len(w.identities) == 3, "任务本身要照常跑完三次尝试"
    assert w.mailbox.close_calls == 0, (
        f"clean_browser_context=false 仍然关了 {w.mailbox.close_calls} 次"
    )


def test_a_failing_close_does_not_abort_the_task(monkeypatch):
    """清理失败不得掩盖注册结果。今天是**绿**的，它拦的是「裸调 close()」。"""
    w = world(monkeypatch, outcomes=["ok", "ok"])
    w.mailbox.close_raises = True
    task = w.run(count=2)

    assert task["status"] == TASK_STATUS_SUCCEEDED, (
        f"close() 抛异常把任务弄失败了: {task['error']!r}"
    )
    assert len(w.saved) == 2
