from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.task_commands import TaskCommandsService
from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["task-commands"])
command_service = TaskCommandsService()
query_service = TasksQueryService()


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "auto"
    extra: dict = Field(default_factory=dict)
    # —— 注册策略（2026-09-21 补）。必须显式声明，否则 pydantic 默认
    # extra='ignore' 会在 model_dump() 这一步把它们静默丢掉（不报错、不记日志），
    # 内核只能拿到默认值 —— 重试次数和两个间隔就永远是 0。
    # 取值范围由 RegistrationStrategy.from_payload 统一校验，这里不重复。
    # 注意：max_attempts 故意不在这里暴露 —— 它的默认值是算出来的
    # (target*(retry+1)*ATTEMPT_BUDGET_FACTOR)，写成带字面默认值的字段会
    # 无条件吐出 0，撞上 from_payload 的下界 1 而让每个任务都失败。
    retry_count: int = 0
    retry_interval_seconds: float = 0.0
    account_interval_seconds: float = 0.0
    phone_retry_count: int = Field(default=2, ge=0, le=10)
    proxy_strategy: str = "round_robin"
    clean_browser_context: bool = True
    require_proxy: bool = False


@router.post("/register")
def create_register_task(body: RegisterTaskRequest):
    return command_service.create_register_task(body.model_dump())


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    task = command_service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        command_service.stream_task_events(task_id, since=since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
