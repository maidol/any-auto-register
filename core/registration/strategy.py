"""注册任务的调度内核：策略参数、任务级代理分配、账号周期执行。

本模块**不导入任何项目内模块**，所有外部能力经构造参数注入，
因此可以脱离数据库、浏览器和付费接码接口单独测试。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

MAX_TARGET_SUCCESS = 50
MAX_RETRY_COUNT = 10
ATTEMPT_BUDGET_FACTOR = 3

CANCELLED = "cancelled"
EXHAUSTED_ATTEMPTS = "attempt_budget_exhausted"
EXHAUSTED_FAILURES = "failed_cycle_budget_exhausted"
COMPLETED = "completed"

PROXY_OK = "ok"
PROXY_FAIL = "fail"
PROXY_NEUTRAL = "neutral"


class StrategyParamError(ValueError):
    """参数不合法 —— 调用方应转成 HTTP 422。"""


@dataclass(frozen=True)
class RegistrationStrategy:
    target_success: int = 1
    retry_count: int = 0
    retry_interval_seconds: float = 0.0
    account_interval_seconds: float = 0.0
    max_attempts: int = 0
    max_failed_cycles: int = 0
    proxy_strategy: str = "round_robin"
    clean_browser_context: bool = True
    require_proxy: bool = False

    @property
    def attempts_per_cycle(self) -> int:
        return self.retry_count + 1

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RegistrationStrategy":
        def _int(name: str, default: int, lo: int, hi: int) -> int:
            raw = payload.get(name, default)
            if raw is None or raw == "":
                raw = default
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise StrategyParamError(f"{name} 必须是整数，收到 {raw!r}")
            if value < lo or value > hi:
                raise StrategyParamError(f"{name} 超出范围 [{lo}, {hi}]，收到 {value}")
            return value

        def _num(
            name: str,
            default: float,
            lo: float,
            hi: float | None = None,
        ) -> float:
            raw = payload.get(name, default)
            if raw is None or raw == "":
                raw = default
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise StrategyParamError(f"{name} 必须是数字，收到 {raw!r}")
            if not math.isfinite(value) or value < lo or (hi is not None and value > hi):
                if hi is None:
                    bounds = f"[{lo}, +∞)"
                else:
                    bounds = f"[{lo}, {hi}]"
                raise StrategyParamError(f"{name} 超出范围 {bounds}，收到 {value}")
            return value

        concurrency = payload.get("concurrency", 1)
        if concurrency not in (None, "", 1, "1"):
            raise StrategyParamError(
                "新注册策略按账号串行执行，concurrency 只能是 1；"
                "需要并发请显式选择 legacy 模式"
            )

        target_success = _int("count", 1, 1, MAX_TARGET_SUCCESS)
        retry_count = _int("retry_count", 0, 0, MAX_RETRY_COUNT)
        default_budget = target_success * (retry_count + 1) * ATTEMPT_BUDGET_FACTOR
        max_attempts = _int("max_attempts", default_budget, 1, 1000)
        max_failed_cycles = _int("max_failed_cycles", 0, 0, 1000)

        strategy_name = str(payload.get("proxy_strategy") or "round_robin").strip()
        if strategy_name not in ("round_robin", "fixed"):
            raise StrategyParamError(f"proxy_strategy 只支持 round_robin / fixed，收到 {strategy_name!r}")

        return cls(
            target_success=target_success,
            retry_count=retry_count,
            retry_interval_seconds=_num("retry_interval_seconds", 0.0, 0.0),
            account_interval_seconds=_num("account_interval_seconds", 0.0, 0.0),
            max_attempts=max_attempts,
            max_failed_cycles=max_failed_cycles,
            proxy_strategy=strategy_name,
            clean_browser_context=bool(payload.get("clean_browser_context", True)),
            require_proxy=bool(payload.get("require_proxy", False)),
        )

    def as_result_dict(self) -> dict[str, Any]:
        return {
            "target_success": self.target_success,
            "retry_count": self.retry_count,
            "retry_interval_seconds": self.retry_interval_seconds,
            "account_interval_seconds": self.account_interval_seconds,
            "max_attempts": self.max_attempts,
            "max_failed_cycles": self.max_failed_cycles,
            "proxy_strategy": self.proxy_strategy,
            "browser_clean": self.clean_browser_context,
        }


class ProxyExhausted(RuntimeError):
    """require_proxy=True 但快照为空。"""


class TaskProxyAllocator:
    """任务开始那一刻对代理池做一次顺序快照，之后只在快照内轮转。

    快照是必须的，不是优化：线上 `ProxyPool.get_next()` 每次调用都按
    success_count/(success+fail) 重新排序，而 report_success / report_fail
    会改这个排序键，所以基于活池的 index 轮询无法保证「账号 2 拿到的不是
    账号 1 那个」。另外 `proxy_pool` 是模块级单例，它的 _index 跨任务共享。
    """

    def __init__(
        self,
        snapshot: Optional[list[str]] = None,
        *,
        fixed_proxy: Optional[str] = None,
        require_proxy: bool = False,
    ) -> None:
        self._fixed = (fixed_proxy or "").strip() or None
        self._snapshot = [str(p).strip() for p in (snapshot or []) if str(p).strip()]
        self._require = bool(require_proxy)
        self._cursor = 0
        if self._require and not self._fixed and not self._snapshot:
            raise ProxyExhausted("require_proxy=true，但代理池为空")

    @property
    def snapshot(self) -> list[str]:
        return list(self._snapshot)

    def next_for_account(self) -> Optional[str]:
        """每个账号周期调用一次 —— 重试不得再调用它。"""
        if self._fixed:
            return self._fixed
        if not self._snapshot:
            return None
        proxy = self._snapshot[self._cursor % len(self._snapshot)]
        self._cursor += 1
        return proxy


@dataclass
class AttemptOutcome:
    """一次 `register_once` 的结果。

    account_saved=True 表示账号已经落库；此时即使 ok=False（后处理失败），
    也**不得重试** —— 重试会再注册一个新账号并再消耗一次付费接码。
    """

    ok: bool
    error: str = ""
    account_saved: bool = False
    proxy_health: str = PROXY_NEUTRAL


@dataclass
class CycleRecord:
    index: int
    proxy: Optional[str]
    attempts: int
    success: bool
    account_saved: bool
    errors: list[str] = field(default_factory=list)
    #: 等待被打断（sleep 返回 False）—— 不依赖 is_cancelled() 再确认一次。
    interrupted: bool = False


@dataclass
class TaskOutcome:
    stop_reason: str = COMPLETED
    attempts: int = 0
    successful_cycles: int = 0
    failed_cycles: int = 0
    cycles: list[CycleRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_result_dict(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "attempts": self.attempts,
            "successful_cycles": self.successful_cycles,
            "failed_cycles": self.failed_cycles,
        }


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


class AccountCycleRunner:
    """串行账号周期 + 周期内重试 + 每周期幂等清理。

    注入点（全部必须由调用方提供真实实现，测试里换成 fake）：
      register_once(index, attempt, proxy) -> AttemptOutcome
      cleanup(index, attempt, proxy)        -> None      每次尝试后必调一次
      sleep(seconds)                        -> bool      返回 False 表示被取消打断
      is_cancelled()                        -> bool
      report_proxy(proxy, health)           -> None      每个周期结束调一次
      on_event(kind, payload)               -> None      日志
    """

    def __init__(
        self,
        strategy: RegistrationStrategy,
        allocator: TaskProxyAllocator,
        *,
        register_once: Callable[[int, int, Optional[str]], AttemptOutcome],
        cleanup: Callable[[int, int, Optional[str]], None],
        sleep: Callable[[float], bool],
        is_cancelled: Callable[[], bool],
        report_proxy: Callable[[Optional[str], str], None] = _noop,
        on_event: Callable[[str, dict[str, Any]], None] = _noop,
    ) -> None:
        self.strategy = strategy
        self.allocator = allocator
        self._register_once = register_once
        self._cleanup = cleanup
        self._sleep = sleep
        self._is_cancelled = is_cancelled
        self._report_proxy = report_proxy
        self._on_event = on_event

    def run(self) -> TaskOutcome:
        s = self.strategy
        out = TaskOutcome()
        index = 0

        while out.successful_cycles < s.target_success:
            if self._is_cancelled():
                out.stop_reason = CANCELLED
                return out
            if out.attempts >= s.max_attempts:
                out.stop_reason = EXHAUSTED_ATTEMPTS
                return out
            if s.max_failed_cycles and out.failed_cycles >= s.max_failed_cycles:
                out.stop_reason = EXHAUSTED_FAILURES
                return out

            proxy = self.allocator.next_for_account()
            record = self._run_cycle(index, proxy, out)
            out.cycles.append(record)
            out.attempts += record.attempts
            if record.success:
                out.successful_cycles += 1
            else:
                out.failed_cycles += 1
                out.errors.extend(record.errors)

            if record.attempts:
                self._report_proxy(proxy, PROXY_OK if record.success else PROXY_FAIL)

            index += 1
            if record.interrupted:
                out.stop_reason = CANCELLED
                return out
            if out.successful_cycles >= s.target_success:
                break
            if self._is_cancelled():
                out.stop_reason = CANCELLED
                return out
            if s.account_interval_seconds > 0 and not self._sleep(s.account_interval_seconds):
                out.stop_reason = CANCELLED
                return out

        return out

    def _run_cycle(self, index: int, proxy: Optional[str], out: TaskOutcome) -> CycleRecord:
        s = self.strategy
        record = CycleRecord(index=index, proxy=proxy, attempts=0, success=False, account_saved=False)

        for attempt in range(s.attempts_per_cycle):
            if self._is_cancelled():
                break
            if out.attempts + record.attempts >= s.max_attempts:
                break

            record.attempts += 1
            self._on_event("attempt_start", {"index": index, "attempt": attempt, "proxy": proxy})
            try:
                outcome = self._register_once(index, attempt, proxy)
            except Exception as exc:  # noqa: BLE001 — 任何异常都只终结本次尝试
                outcome = AttemptOutcome(ok=False, error=str(exc) or exc.__class__.__name__)
            finally:
                self._safe_cleanup(index, attempt, proxy)

            if outcome.account_saved:
                record.account_saved = True

            if outcome.ok:
                record.success = True
                return record

            record.errors.append(outcome.error or "unknown error")

            if outcome.account_saved:
                # 账号已经落库，失败在后处理 —— 周期到此为止，不再注册新账号。
                self._on_event("post_processing_failed", {"index": index, "error": outcome.error})
                record.success = True
                return record

            is_last = attempt >= s.retry_count
            if is_last:
                break
            if self._is_cancelled():
                break
            if s.retry_interval_seconds > 0 and not self._sleep(s.retry_interval_seconds):
                record.interrupted = True
                break

        return record

    def _safe_cleanup(self, index: int, attempt: int, proxy: Optional[str]) -> None:
        """清理异常不得掩盖主异常，也不得中断调度。"""
        try:
            self._cleanup(index, attempt, proxy)
        except Exception as exc:  # noqa: BLE001
            self._on_event("cleanup_failed", {"index": index, "attempt": attempt, "error": str(exc)})
