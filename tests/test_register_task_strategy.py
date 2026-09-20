"""`_execute_register_task` 接上调度内核之后的行为。

这些测试把注册任务的所有外部副作用（平台实例、落库、代理池、日志表）
换成假的，只留调度语义本身，所以不需要浏览器、接码接口和真实代理。
"""
from __future__ import annotations

import pytest

from application.tasks import (
    TASK_STATUS_CANCEL_REQUESTED,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCEEDED,
    TaskLogger,
    _execute_register_task,
    _mutate_task,
    create_task,
    get_task,
)


class FakeAccount:
    def __init__(self, email: str) -> None:
        self.email = email
        self.password = "pw"
        self.platform = "chatgpt"
        self.extra: dict = {}
        self.user_id = ""
        self.token = ""


class FakePlatform:
    """每次 `_build_platform_instance` 造一个新的，register 的行为由 world 决定。"""

    def __init__(self, world: "FakeWorld", proxy) -> None:
        self.world = world
        self.proxy = proxy

    def register(self, email=None, password=None):
        w = self.world
        attempt_no = len(w.register_calls)
        w.register_calls.append(self.proxy)
        if w.cancel_after_registers is not None and attempt_no + 1 >= w.cancel_after_registers:
            w.request_cancel()
        outcome = w.outcomes[attempt_no] if attempt_no < len(w.outcomes) else w.default_outcome
        if outcome != "ok":
            raise RuntimeError(outcome)
        return FakeAccount(f"acct{attempt_no}@example.com")

    def set_logger(self, _fn):
        return None


class FakeProxyPool:
    def __init__(self, urls: list[str]) -> None:
        self.urls = list(urls)
        self._i = 0
        self.reports: list[tuple[str, str]] = []
        self.get_next_calls = 0

    def get_next(self, region: str = ""):
        self.get_next_calls += 1
        if not self.urls:
            return None
        url = self.urls[self._i % len(self.urls)]
        self._i += 1
        return url

    def report_success(self, url):
        self.reports.append((url, "ok"))

    def report_fail(self, url):
        self.reports.append((url, "fail"))


class FakeWorld:
    def __init__(self, monkeypatch, *, outcomes, proxies, default_outcome="boom") -> None:
        self.outcomes = list(outcomes)
        self.default_outcome = default_outcome
        self.register_calls: list = []
        self.saved: list[FakeAccount] = []
        self.pool = FakeProxyPool(proxies)
        self.cancel_after_registers = None
        self.post_processing_raises = False
        self.task = create_task(task_type="register", platform="chatgpt", payload={})
        self.task_id = self.task["id"]
        self.logger = TaskLogger(self.task_id)

        def _build(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
            return FakePlatform(self, resolved_proxy)

        def _save(account):
            self.saved.append(account)

        def _followup(**kwargs):
            if self.post_processing_raises:
                raise RuntimeError("cashier link write failed")

        monkeypatch.setattr("application.tasks._build_platform_instance", _build)
        monkeypatch.setattr("application.tasks.save_account", _save)
        monkeypatch.setattr("application.tasks._save_task_log", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks._auto_followup_windsurf_payment", _followup)
        monkeypatch.setattr("application.tasks._auto_upload_cpa", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks._auto_push_any2api", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks.get", lambda _name: FakePlatform)
        monkeypatch.setattr("core.proxy_pool.proxy_pool", self.pool)
        monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **kw: object())
        monkeypatch.setattr(
            "infrastructure.provider_settings_repository.ProviderSettingsRepository"
            ".get_default_provider_key",
            lambda self, kind: "moemail",
        )

    def request_cancel(self):
        _mutate_task(self.task_id, lambda m: setattr(m, "status", TASK_STATUS_CANCEL_REQUESTED))

    def run(self, **payload):
        body = {"platform": "chatgpt"}
        body.update(payload)
        _execute_register_task(body, self.logger)
        return get_task(self.task_id)

    @property
    def proxies_used(self):
        return list(self.register_calls)


def world(monkeypatch, *, outcomes=(), proxies=("P1", "P2", "P3"), default_outcome="boom"):
    return FakeWorld(monkeypatch, outcomes=outcomes, proxies=list(proxies), default_outcome=default_outcome)


# --------------------------------------------------------------------------
# B1 —— count 是成功目标，不是尝试数；预算有限
# --------------------------------------------------------------------------

def test_count_is_a_success_target_not_an_attempt_count(monkeypatch):
    """两次失败夹在中间，count=2 仍要拿到 2 个账号。

    今天 count 是尝试数：跑 2 次、两次都失败就收工，saved 为空。
    """
    w = world(monkeypatch, outcomes=["boom", "ok", "boom", "ok"])
    task = w.run(count=2, retry_count=2)

    assert len(w.saved) == 2
    assert len(w.register_calls) == 4
    assert task["status"] == TASK_STATUS_SUCCEEDED


def test_attempt_budget_stops_a_task_that_never_succeeds(monkeypatch):
    """永远失败时必须在预算处停下，而不是无限转。

    默认预算 = count × (retry_count + 1) × 3 = 2 × 1 × 3 = 6。
    """
    w = world(monkeypatch, outcomes=[])
    task = w.run(count=2, retry_count=0)

    assert len(w.register_calls) == 6
    assert task["status"] == TASK_STATUS_FAILED
    assert task["data"]["stop_reason"] == "attempt_budget_exhausted"


def test_explicit_max_attempts_is_honoured(monkeypatch):
    w = world(monkeypatch, outcomes=[])
    task = w.run(count=5, retry_count=3, max_attempts=4)

    assert len(w.register_calls) == 4
    assert task["data"]["attempts"] == 4


def test_max_failed_cycles_stops_earlier_than_the_attempt_budget(monkeypatch):
    w = world(monkeypatch, outcomes=[])
    task = w.run(count=5, retry_count=1, max_failed_cycles=2)

    # 2 个失败周期 × 每周期 2 次尝试 = 4
    assert len(w.register_calls) == 4
    assert task["data"]["stop_reason"] == "failed_cycle_budget_exhausted"


# --------------------------------------------------------------------------
# B2 —— 代理按账号周期分配、按周期上报
# --------------------------------------------------------------------------

def test_all_attempts_of_one_account_share_one_proxy(monkeypatch):
    """今天每次尝试都 get_next()，所以同一个账号的重试会换代理。"""
    w = world(monkeypatch, outcomes=["boom", "boom", "ok", "ok"], proxies=["P1", "P2"])
    w.run(count=2, retry_count=2)

    # 账号 0：三次尝试（失败、失败、成功）全用 P1；账号 1 换到 P2
    assert w.proxies_used == ["P1", "P1", "P1", "P2"]


def test_proxy_health_is_reported_once_per_cycle_not_once_per_attempt(monkeypatch):
    """今天每次失败都 report_fail，三次重试把同一个代理推向自动禁用阈值。"""
    w = world(monkeypatch, outcomes=["boom", "boom", "ok", "ok"], proxies=["P1", "P2"])
    w.run(count=2, retry_count=2)

    assert w.pool.reports == [("P1", "ok"), ("P2", "ok")]


def test_a_cycle_that_never_succeeds_reports_its_proxy_failed_once(monkeypatch):
    w = world(monkeypatch, outcomes=[], proxies=["P1", "P2"])
    w.run(count=1, retry_count=1, max_attempts=4)

    assert w.pool.reports == [("P1", "fail"), ("P2", "fail")]


def test_payload_proxy_overrides_the_pool_entirely(monkeypatch):
    w = world(monkeypatch, outcomes=["ok", "ok"], proxies=["P1", "P2"])
    w.run(count=2, proxy="http://fixed:8080")

    assert w.proxies_used == ["http://fixed:8080", "http://fixed:8080"]
    assert w.pool.get_next_calls == 0


def test_require_proxy_with_an_empty_pool_fails_the_task_before_registering(monkeypatch):
    w = world(monkeypatch, outcomes=["ok"], proxies=[])
    task = w.run(count=1, require_proxy=True)

    assert w.register_calls == []
    assert task["status"] == TASK_STATUS_FAILED


def test_an_empty_pool_without_require_proxy_registers_directly(monkeypatch):
    w = world(monkeypatch, outcomes=["ok"], proxies=[])
    task = w.run(count=1)

    assert w.proxies_used == [None]
    assert task["status"] == TASK_STATUS_SUCCEEDED


# --------------------------------------------------------------------------
# B7 —— 账号落库之后不得再注册
# --------------------------------------------------------------------------

def test_post_processing_failure_does_not_register_a_second_account(monkeypatch):
    """落库之后后处理抛异常，本周期结束，不得再消耗一次付费接码。"""
    w = world(monkeypatch, outcomes=["ok"])
    w.post_processing_raises = True
    task = w.run(count=1, retry_count=2)

    assert len(w.register_calls) == 1
    assert len(w.saved) == 1
    assert task["status"] == TASK_STATUS_SUCCEEDED


# --------------------------------------------------------------------------
# 串行 / 取消 / 参数
# --------------------------------------------------------------------------

def test_concurrency_greater_than_one_fails_the_task_instead_of_being_clamped(monkeypatch):
    """今天 concurrency=3 被静默 clamp 到 min(3, count, 5)，用户以为并发生效了。"""
    w = world(monkeypatch, outcomes=["ok", "ok", "ok"])
    task = w.run(count=3, concurrency=3)

    assert w.register_calls == []
    assert task["status"] == TASK_STATUS_FAILED
    assert "concurrency" in (task["error"] or "")


def test_cancel_between_accounts_stops_the_task(monkeypatch):
    w = world(monkeypatch, outcomes=["ok", "ok", "ok"])
    w.cancel_after_registers = 1
    task = w.run(count=3)

    assert len(w.register_calls) == 1
    assert task["status"] == TASK_STATUS_CANCELLED


def test_account_interval_is_interruptible(monkeypatch):
    """取消请求必须打断账号间隔，而不是睡满再看。"""
    import time

    w = world(monkeypatch, outcomes=["ok", "ok"])
    w.cancel_after_registers = 1
    started = time.monotonic()
    task = w.run(count=2, account_interval_seconds=30)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert task["status"] == TASK_STATUS_CANCELLED


def test_a_cancel_arriving_during_the_interval_interrupts_the_sleep(monkeypatch):
    """取消在睡眠**中途**到达。

    上面那条 `..._is_interruptible` 的取消在进入睡眠之前就可见，调度层在
    睡觉前那一次 is_cancelled() 就返回了 —— 它区分不了「睡眠被打断」和
    「还没开始睡就发现了」。把 time.sleep 换成不可打断的整段睡眠，
    那一条照样绿，只有这一条会红。
    """
    import threading
    import time

    w = world(monkeypatch, outcomes=["ok", "ok"])
    timer = threading.Timer(0.4, w.request_cancel)
    timer.start()
    started = time.monotonic()
    try:
        task = w.run(count=2, account_interval_seconds=12)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started

    assert elapsed < 6, f"睡眠没有被打断，整整睡了 {elapsed:.1f} 秒"
    assert len(w.register_calls) == 1
    assert task["status"] == TASK_STATUS_CANCELLED


def test_account_interval_is_actually_applied_between_accounts(monkeypatch):
    """上一条只证明「能被打断」，证明不了「真的睡了」—— 今天没有间隔逻辑，它空过。"""
    import time

    w = world(monkeypatch, outcomes=["ok", "ok"])
    started = time.monotonic()
    w.run(count=2, account_interval_seconds=0.6)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.6
    assert len(w.register_calls) == 2


def test_retry_interval_is_applied_between_attempts_of_one_account(monkeypatch):
    import time

    w = world(monkeypatch, outcomes=["boom", "ok"])
    started = time.monotonic()
    w.run(count=1, retry_count=1, retry_interval_seconds=0.6)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.6
    assert len(w.register_calls) == 2


def test_herosms_keeps_going_for_bonus_accounts_while_the_number_is_alive(monkeypatch):
    """号码仍可复用时，超过 count 的额外成功要继续拿 —— 这是付费功能，不能在重构里丢掉。"""
    alive = {"n": 2}

    def _fake_alive(_settings):
        if alive["n"] > 0:
            alive["n"] -= 1
            return True, {"phone_number": "1234567890", "remaining_seconds": 60, "use_count": 1}
        return False, {}

    monkeypatch.setattr("core.base_sms.is_herosms_phone_cache_alive", _fake_alive)
    w = world(monkeypatch, outcomes=["ok"] * 8)
    monkeypatch.setattr(
        "application.tasks._resolve_sms_provider_for_task",
        lambda extra: ("herosms", {"herosms_api_key": "k"}),
    )
    monkeypatch.setattr("application.tasks._hero_task_reuse_policy", lambda k, s: (True, 3))
    task = w.run(count=1)

    # 目标 1 个 + 号码活着期间再补 2 个
    assert len(w.saved) == 3
    assert task["status"] == TASK_STATUS_SUCCEEDED


def test_result_data_records_the_strategy_and_the_stop_reason(monkeypatch):
    w = world(monkeypatch, outcomes=["ok", "ok"])
    task = w.run(count=2, retry_count=1, account_interval_seconds=0)
    data = task["data"]

    assert data["stop_reason"] == "completed"
    assert data["successful_cycles"] == 2
    assert data["attempts"] == 2
    assert data["strategy"]["target_success"] == 2
    assert data["strategy"]["retry_count"] == 1


def test_progress_and_success_count_track_saved_accounts_not_attempts(monkeypatch):
    """今天 progress 走的是 completed（尝试数），所以两次失败也会显示 2/2。"""
    w = world(monkeypatch, outcomes=["boom", "boom", "ok", "ok"])
    task = w.run(count=2, retry_count=2)

    assert task["success"] == 2
    assert task["progress_detail"]["current"] == 2
    assert task["progress_detail"]["total"] == 2
