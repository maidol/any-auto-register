"""HTTP 边界回归测试：策略参数必须活着穿过 RegisterTaskRequest。

为什么需要这个文件：`tests/test_registration_strategy.py` 和
`tests/test_register_task_strategy.py` 一共 44 条，全部把**手写 dict**
喂给 `RegistrationStrategy.from_payload()`。那正是 pydantic 会丢掉的那些键，
在那些测试里它们当然都在。边界两侧各自自洽，而没有任何一条测试穿过边界。

2026-09-21 的生产事故就是这么来的：`RegisterTaskRequest` 没声明策略字段，
pydantic 默认 extra='ignore' 把它们静默丢掉（不报错、不记日志），
于是重试次数、重试间隔、账号间隔在生产里恒为 0，而 44 条测试全绿。

`customer_portal_api` 的两份同名模型为什么只做 AST 检查而不导入：
那个包内部混用了 `app.*` 与 `customer_portal_api.app.*` 两个 import 根
（6 个文件走前者），同进程加载两份会撞 SQLModel 重复建表，
而单独走任一根都 ModuleNotFoundError —— 它自引入提交起就无法启动。
在它被修好之前，这里只能检查字段声明是否同步。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from api.task_commands import RegisterTaskRequest
from core.registration.strategy import (
    ATTEMPT_BUDGET_FACTOR,
    RegistrationStrategy,
    StrategyParamError,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 前端真正会发的那组参数。
FRONTEND_BODY = {
    "platform": "chatgpt",
    "count": 3,
    "concurrency": 1,
    "executor_type": "browser",
    "captcha_solver": "auto",
    "proxy": None,
    "extra": {"identity_provider": "mailbox"},
    # —— 以下是策略参数，2026-09-21 之前全部在这一层被丢掉 ——
    "retry_count": 2,
    "retry_interval_seconds": 15,
    "account_interval_seconds": 10,
    "proxy_strategy": "round_robin",
    "clean_browser_context": True,
    "require_proxy": False,
}

STRATEGY_KEYS = (
    "retry_count",
    "retry_interval_seconds",
    "account_interval_seconds",
    "proxy_strategy",
    "clean_browser_context",
    "require_proxy",
)


def test_strategy_keys_survive_model_dump():
    """声明缺失时 model_dump() 会少键 —— 这是丢弃发生的那一刻。"""
    dumped = RegisterTaskRequest(**FRONTEND_BODY).model_dump()
    missing = [k for k in STRATEGY_KEYS if k not in dumped]
    assert not missing, (
        f"RegisterTaskRequest 丢掉了策略字段 {missing}；"
        "pydantic 默认 extra='ignore'，未声明的键在 model_dump() 这一步被静默丢弃"
    )


def test_strategy_values_reach_the_kernel_unchanged():
    """端到端：前端发什么，内核就该拿到什么。"""
    strategy = RegistrationStrategy.from_payload(
        RegisterTaskRequest(**FRONTEND_BODY).model_dump()
    )
    assert strategy.target_success == 3
    assert strategy.retry_count == 2
    assert strategy.attempts_per_cycle == 3
    assert strategy.retry_interval_seconds == 15.0
    assert strategy.account_interval_seconds == 10.0
    assert strategy.proxy_strategy == "round_robin"
    assert strategy.clean_browser_context is True


def test_omitted_strategy_params_fall_back_to_kernel_defaults():
    """老客户端（不发策略字段）行为不变：单次尝试、零间隔。"""
    legacy_body = {k: v for k, v in FRONTEND_BODY.items() if k not in STRATEGY_KEYS}
    strategy = RegistrationStrategy.from_payload(
        RegisterTaskRequest(**legacy_body).model_dump()
    )
    assert strategy.retry_count == 0
    assert strategy.attempts_per_cycle == 1
    assert strategy.retry_interval_seconds == 0.0
    assert strategy.account_interval_seconds == 0.0


def test_max_attempts_stays_computed_and_is_never_emitted_as_zero():
    """地雷钉子：max_attempts 不许被声明成带字面默认值的字段。

    内核算的是 target*(retry+1)*ATTEMPT_BUDGET_FACTOR，而 from_payload 里
    `_int("max_attempts", default_budget, 1, 1000)` 的下界是 1。
    一旦请求模型写成 `max_attempts: int = 0`，model_dump() 就会无条件吐出 0，
    下界检查当场抛 StrategyParamError —— **每一个注册任务都会失败**。
    要暴露它只能用 Optional[int] = None。
    """
    dumped = RegisterTaskRequest(**FRONTEND_BODY).model_dump()
    assert dumped.get("max_attempts") != 0, (
        "max_attempts 被声明成了带字面默认值 0 的字段，"
        "会让 from_payload 的下界检查对每一个任务都抛 StrategyParamError"
    )
    strategy = RegistrationStrategy.from_payload(dumped)
    assert strategy.max_attempts == 3 * (2 + 1) * ATTEMPT_BUDGET_FACTOR


def test_out_of_range_retry_count_is_rejected_not_silently_clamped():
    """超范围要抛 StrategyParamError，不能被悄悄夹住或丢掉。"""
    body = dict(FRONTEND_BODY, retry_count=999)
    with pytest.raises(StrategyParamError):
        RegistrationStrategy.from_payload(RegisterTaskRequest(**body).model_dump())


def test_large_intervals_survive_http_boundary_unchanged():
    body = dict(
        FRONTEND_BODY,
        retry_interval_seconds=3601,
        account_interval_seconds=999999,
    )
    strategy = RegistrationStrategy.from_payload(
        RegisterTaskRequest(**body).model_dump()
    )

    assert strategy.retry_interval_seconds == 3601.0
    assert strategy.account_interval_seconds == 999999.0


# --- customer_portal_api 的两份同名模型：AST 平价检查 -----------------------

PORTAL_MODELS = [
    "customer_portal_api/app/routers/admin.py",
    "customer_portal_api/app/routers/app_api.py",
]


def _declared_fields(rel_path: str, class_name: str) -> set[str]:
    tree = ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"{rel_path} 里找不到 class {class_name}")


@pytest.mark.parametrize("rel_path", PORTAL_MODELS)
def test_portal_request_models_declare_the_same_strategy_fields(rel_path):
    """三个入口共用一套策略字段，任何一个漏声明就会在那个入口上静默丢参数。

    这里只能做声明层面的检查（见模块 docstring：portal 包导不进来）。
    **它证明不了 pydantic 的实际行为** —— 只证明字段名在源码里同步了。
    portal 恢复可导入之后，这条应当换成和上面一样的真实 model_dump() 断言。
    """
    declared = _declared_fields(rel_path, "RegisterTaskRequest")
    missing = [k for k in STRATEGY_KEYS if k not in declared]
    assert not missing, f"{rel_path} 的 RegisterTaskRequest 少声明了 {missing}"
