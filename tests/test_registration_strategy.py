"""调度内核的语义测试 —— 全部用 fake 依赖，不碰数据库/浏览器/接码。"""
from __future__ import annotations

import pytest

from core.registration.strategy import (
    CANCELLED,
    COMPLETED,
    EXHAUSTED_ATTEMPTS,
    EXHAUSTED_FAILURES,
    PROXY_FAIL,
    PROXY_OK,
    AccountCycleRunner,
    AttemptOutcome,
    ProxyExhausted,
    RegistrationStrategy,
    StrategyParamError,
    TaskProxyAllocator,
)


class RunawayLoop(BaseException):
    """runner 没有终止 —— 用异常代替挂死，否则失去预算守卫只会让测试超时。

    必须继承 BaseException 而不是 Exception：`AccountCycleRunner._run_cycle`
    对每次尝试做 `except Exception`，继承 Exception 的守卫会被它当成
    「这次尝试失败了」吞掉，循环照样无限转下去，测试从「红」退化成「挂死」。
    """


class FakeWorld:
    """记录调用顺序的假世界。script 决定第 n 次尝试的结果。"""

    #: 任何一个测试里 register_once 的调用次数都远低于它；超过即判定失控。
    HARD_CALL_CAP = 200

    def __init__(self, script, *, cancel_after=None, sleep_returns=None):
        self.script = list(script)
        self.calls = []          # (index, attempt, proxy)
        self.cleanups = []       # (index, attempt, proxy)
        self.sleeps = []         # 秒数
        self.proxy_reports = []  # (proxy, health)
        self.events = []
        self._cancel_after = cancel_after
        self._sleep_returns = list(sleep_returns or [])
        self._n = 0

    def register_once(self, index, attempt, proxy):
        if len(self.calls) >= self.HARD_CALL_CAP:
            raise RunawayLoop(
                f"register_once 被调用超过 {self.HARD_CALL_CAP} 次 —— 调度循环没有上界"
            )
        self.calls.append((index, attempt, proxy))
        item = self.script[self._n] if self._n < len(self.script) else AttemptOutcome(ok=False, error="script exhausted")
        self._n += 1
        if isinstance(item, Exception):
            raise item
        return item

    def cleanup(self, index, attempt, proxy):
        self.cleanups.append((index, attempt, proxy))

    def sleep(self, seconds):
        """返回 False = 等待被打断。

        `sleep_returns` 让「等待被打断」和「任务已取消」成为两件独立的事，
        否则断言分不清 runner 是看了返回值还是下一轮才发现取消。
        """
        self.sleeps.append(seconds)
        if self._sleep_returns:
            return self._sleep_returns.pop(0)
        return not self.is_cancelled()

    def is_cancelled(self):
        return self._cancel_after is not None and len(self.calls) >= self._cancel_after

    def report_proxy(self, proxy, health):
        self.proxy_reports.append((proxy, health))

    def on_event(self, kind, payload):
        self.events.append((kind, payload))


def build(strategy, world, proxies=("P1", "P2", "P3")):
    return AccountCycleRunner(
        strategy,
        TaskProxyAllocator(list(proxies)),
        register_once=world.register_once,
        cleanup=world.cleanup,
        sleep=world.sleep,
        is_cancelled=world.is_cancelled,
        report_proxy=world.report_proxy,
        on_event=world.on_event,
    )


OK = AttemptOutcome(ok=True)


def fail(msg="boom"):
    return AttemptOutcome(ok=False, error=msg)


# --- 1. 参数规范化与拒绝 -------------------------------------------------

def test_concurrency_greater_than_one_is_rejected():
    with pytest.raises(StrategyParamError) as exc:
        RegistrationStrategy.from_payload({"count": 3, "concurrency": 2})
    assert "concurrency" in str(exc.value)


def test_concurrency_one_and_missing_are_both_accepted():
    assert RegistrationStrategy.from_payload({"count": 2}).target_success == 2
    assert RegistrationStrategy.from_payload({"count": 2, "concurrency": 1}).target_success == 2


def test_out_of_range_params_are_rejected():
    with pytest.raises(StrategyParamError):
        RegistrationStrategy.from_payload({"count": 0})
    with pytest.raises(StrategyParamError):
        RegistrationStrategy.from_payload({"count": 1, "retry_count": -1})
    with pytest.raises(StrategyParamError):
        RegistrationStrategy.from_payload({"count": 1, "retry_interval_seconds": -1})
    with pytest.raises(StrategyParamError):
        RegistrationStrategy.from_payload({"count": 1, "account_interval_seconds": -1})
    with pytest.raises(StrategyParamError):
        RegistrationStrategy.from_payload({"count": 1, "proxy_strategy": "random"})


def test_intervals_above_one_hour_are_accepted():
    strategy = RegistrationStrategy.from_payload(
        {
            "count": 1,
            "retry_interval_seconds": 3601,
            "account_interval_seconds": 999999,
        }
    )

    assert strategy.retry_interval_seconds == 3601.0
    assert strategy.account_interval_seconds == 999999.0


def test_attempt_budget_defaults_to_three_times_the_nominal_work():
    s = RegistrationStrategy.from_payload({"count": 3, "retry_count": 2})
    assert s.max_attempts == 3 * 3 * 3


# --- 2. 成功目标 ---------------------------------------------------------

def test_runs_until_target_success_not_until_target_attempts():
    world = FakeWorld([fail(), fail(), OK, OK, fail(), OK])
    s = RegistrationStrategy.from_payload({"count": 3, "retry_count": 0})
    out = build(s, world).run()
    assert out.successful_cycles == 3
    assert out.failed_cycles == 3
    assert out.attempts == 6
    assert out.stop_reason == COMPLETED


def test_attempt_budget_stops_an_otherwise_unbounded_task():
    world = FakeWorld([fail()] * 100)
    s = RegistrationStrategy.from_payload({"count": 3, "retry_count": 0, "max_attempts": 7})
    out = build(s, world).run()
    assert out.stop_reason == EXHAUSTED_ATTEMPTS
    assert out.attempts == 7
    assert out.successful_cycles == 0


def test_failed_cycle_budget_stops_earlier_than_attempt_budget():
    world = FakeWorld([fail()] * 100)
    s = RegistrationStrategy.from_payload(
        {"count": 3, "retry_count": 1, "max_attempts": 50, "max_failed_cycles": 2}
    )
    out = build(s, world).run()
    assert out.stop_reason == EXHAUSTED_FAILURES
    assert out.failed_cycles == 2
    assert out.attempts == 4


# --- 3. 同账号重试次数与间隔 ---------------------------------------------

def test_retry_count_two_means_three_attempts_for_one_account():
    world = FakeWorld([fail(), fail(), OK])
    s = RegistrationStrategy.from_payload(
        {"count": 1, "retry_count": 2, "retry_interval_seconds": 15}
    )
    out = build(s, world).run()
    assert [c[1] for c in world.calls] == [0, 1, 2]
    assert {c[0] for c in world.calls} == {0}
    assert world.sleeps == [15, 15]
    assert out.successful_cycles == 1
    assert out.attempts == 3


def test_no_retry_interval_after_the_last_attempt():
    world = FakeWorld([fail(), fail()])
    s = RegistrationStrategy.from_payload(
        {"count": 1, "retry_count": 1, "retry_interval_seconds": 15, "max_attempts": 2}
    )
    build(s, world).run()
    assert world.sleeps == [15]


def test_raised_exception_counts_as_a_failed_attempt_not_a_crash():
    world = FakeWorld([RuntimeError("driver died"), OK])
    s = RegistrationStrategy.from_payload({"count": 1, "retry_count": 1})
    out = build(s, world).run()
    assert out.successful_cycles == 1
    assert out.attempts == 2


# --- 4. 账号间隔 ---------------------------------------------------------

def test_account_interval_applies_after_success_and_after_final_failure():
    world = FakeWorld([OK, fail(), OK])
    s = RegistrationStrategy.from_payload(
        {"count": 2, "retry_count": 0, "account_interval_seconds": 10}
    )
    build(s, world).run()
    assert world.sleeps == [10, 10]


def test_no_account_interval_after_the_final_success():
    world = FakeWorld([OK, OK])
    s = RegistrationStrategy.from_payload(
        {"count": 2, "retry_count": 0, "account_interval_seconds": 10}
    )
    build(s, world).run()
    assert world.sleeps == [10]


# --- 5. 代理：周期内固定，周期间推进 -------------------------------------

def test_all_attempts_of_one_account_share_one_proxy_and_next_account_advances():
    world = FakeWorld([fail(), fail(), fail(), OK])
    s = RegistrationStrategy.from_payload({"count": 1, "retry_count": 2})
    build(s, world, proxies=("P1", "P2", "P3")).run()
    assert [c[2] for c in world.calls] == ["P1", "P1", "P1", "P2"]


def test_proxy_snapshot_wraps_around():
    alloc = TaskProxyAllocator(["P1", "P2"])
    assert [alloc.next_for_account() for _ in range(5)] == ["P1", "P2", "P1", "P2", "P1"]


def test_empty_pool_yields_direct_connection_unless_require_proxy():
    assert TaskProxyAllocator([]).next_for_account() is None
    with pytest.raises(ProxyExhausted):
        TaskProxyAllocator([], require_proxy=True)


def test_fixed_proxy_overrides_the_snapshot_for_every_account():
    alloc = TaskProxyAllocator(["P1", "P2"], fixed_proxy="http://fixed:1")
    assert [alloc.next_for_account() for _ in range(3)] == ["http://fixed:1"] * 3


# --- 6. 代理健康上报：按周期一次，不是按尝试一次 -------------------------

def test_proxy_is_reported_once_per_cycle_not_once_per_attempt():
    world = FakeWorld([fail(), fail(), fail(), OK])
    s = RegistrationStrategy.from_payload({"count": 1, "retry_count": 2})
    build(s, world, proxies=("P1", "P2")).run()
    assert world.proxy_reports == [("P1", PROXY_FAIL), ("P2", PROXY_OK)]


# --- 7. 清理 -------------------------------------------------------------

def test_cleanup_runs_once_per_attempt_on_success_failure_and_exception():
    world = FakeWorld([fail(), RuntimeError("x"), OK])
    s = RegistrationStrategy.from_payload({"count": 1, "retry_count": 2})
    build(s, world).run()
    assert world.cleanups == [(0, 0, "P1"), (0, 1, "P1"), (0, 2, "P1")]


def test_cleanup_exception_does_not_mask_the_attempt_result():
    world = FakeWorld([OK])

    def exploding_cleanup(index, attempt, proxy):
        raise RuntimeError("close() failed")

    world.cleanup = exploding_cleanup
    s = RegistrationStrategy.from_payload({"count": 1, "retry_count": 0})
    out = build(s, world).run()
    assert out.successful_cycles == 1
    assert any(kind == "cleanup_failed" for kind, _ in world.events)


# --- 8. 取消 -------------------------------------------------------------

def test_cancel_stops_before_the_next_attempt_and_reports_cancelled():
    world = FakeWorld([fail(), fail(), fail()], cancel_after=2)
    s = RegistrationStrategy.from_payload({"count": 5, "retry_count": 0})
    out = build(s, world).run()
    assert len(world.calls) == 2
    assert out.stop_reason == CANCELLED


def test_an_interrupted_wait_stops_the_task_even_if_cancel_is_not_yet_visible():
    """sleep 返回 False 必须被当真。

    这里 is_cancelled() 全程为 False —— 唯一的信号是 sleep 的返回值。
    盲 sleep（`time.sleep(n)` 后不看返回值）会继续跑第二个账号，
    于是 calls 变成 2、stop_reason 变成 COMPLETED/别的，测试变红。
    """
    world = FakeWorld([fail(), fail()], sleep_returns=[False])
    s = RegistrationStrategy.from_payload(
        {"count": 5, "retry_count": 0, "account_interval_seconds": 300}
    )
    out = build(s, world).run()
    assert world.sleeps == [300]
    assert len(world.calls) == 1
    assert out.stop_reason == CANCELLED


def test_an_interrupted_retry_wait_ends_the_cycle_without_another_attempt():
    world = FakeWorld([fail(), fail(), fail()], sleep_returns=[False])
    s = RegistrationStrategy.from_payload(
        {"count": 1, "retry_count": 2, "retry_interval_seconds": 15}
    )
    build(s, world).run()
    assert world.sleeps == [15]
    assert len(world.calls) == 1


def test_cancel_during_an_interval_aborts_the_sleep():
    world = FakeWorld([fail(), fail()], cancel_after=1)
    s = RegistrationStrategy.from_payload(
        {"count": 5, "retry_count": 0, "account_interval_seconds": 300}
    )
    out = build(s, world).run()
    assert out.stop_reason == CANCELLED
    assert len(world.calls) == 1


# --- 9. 后处理失败不得重新注册 -------------------------------------------

def test_post_processing_failure_does_not_trigger_a_second_registration():
    world = FakeWorld([AttemptOutcome(ok=False, error="cashier 超时", account_saved=True), OK])
    s = RegistrationStrategy.from_payload({"count": 1, "retry_count": 2})
    out = build(s, world).run()
    assert len(world.calls) == 1
    assert out.successful_cycles == 1
    assert out.attempts == 1
    assert any(kind == "post_processing_failed" for kind, _ in world.events)
