"""运维脚本：重置账号复用状态。

用法（数据库位置跟随 ACCOUNT_MANAGER_DATABASE_URL，与服务相同）：
  # 试运行（默认，只打印不写库）
  .venv311/bin/python tools/reset_account_reuse.py a@example.com b@example.com

  # 写入生效（清零复用计数）
  .venv311/bin/python tools/reset_account_reuse.py --apply a@example.com b@example.com

  # 针对注册失败行，重置计数并指定失败阶段
  .venv311/bin/python tools/reset_account_reuse.py --stage created_oauth_failed --apply a@example.com
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from core import db as core_db
from core.account_graph import _graph_for_account, patch_account_graph
from core.db import AccountModel
from core.registration.errors import FAILURE_CREATED, FAILURE_NOT_CREATED, FAILURE_OAUTH

STAGES = (FAILURE_NOT_CREATED, FAILURE_CREATED, FAILURE_OAUTH)


def reset_account(
    session: Session,
    model: AccountModel,
    *,
    stage: str | None,
    apply: bool,
) -> str:
    """处理单个账号模型，返回一行描述文本。"""
    graph = _graph_for_account(session, int(model.id))
    overview = graph.get("overview") or {}
    lifecycle = str(graph.get("lifecycle_status") or "")
    validity = str(graph.get("validity_status") or "")
    origin = str(overview.get("account_origin") or "")
    label = f"#{model.id} {model.email} [{lifecycle}]"

    if lifecycle == "disabled":
        return f"跳过 {label}：已停用账号不重置"

    is_failed = (lifecycle == "failed")
    is_manual_invalid = (
        lifecycle not in ("failed", "disabled")
        and validity == "invalid"
        and origin == "manual"
        and bool(model.password)
    )

    if not is_failed and not is_manual_invalid:
        if not bool(model.password):
            return f"跳过 {label}：账号无密码无法复用"
        return f"跳过 {label}：不是注册失败或手动添加的无效账号"

    summary_updates: dict = {}
    actions: list[str] = []

    if is_failed:
        summary_updates["reuse_attempts"] = 0
        actions.append("reuse_attempts=0")
        if stage:
            old_stage = str(overview.get("failure_stage") or "未设置")
            summary_updates["failure_stage"] = stage
            actions.append(f"failure_stage: {old_stage} → {stage}")
    else:  # is_manual_invalid
        summary_updates["manual_reuse_attempts"] = 0
        actions.append("manual_reuse_attempts=0")
        if stage:
            actions.append("（跳过 stage 指定：仅失败行可指定）")

    details = "，".join(actions)
    if not apply:
        return f"将重置 {label}：{details}"

    patch_account_graph(session, model, summary_updates=summary_updates)
    session.commit()
    return f"已重置 {label}：{details}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重置账号复用状态（清零复用计数）")
    parser.add_argument("--stage", choices=STAGES, default=None, help="失败阶段（仅对注册失败行生效）")
    parser.add_argument("--platform", default="chatgpt", help="平台，默认 chatgpt")
    parser.add_argument("--apply", action="store_true", help="真的写库；不加只打印")
    parser.add_argument("emails", nargs="+", help="要处理的邮箱列表")
    args = parser.parse_args(argv)

    with Session(core_db.engine) as session:
        for raw_email in args.emails:
            email = raw_email.strip()
            if not email:
                continue
            models = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == args.platform)
                .where(AccountModel.email == email)
            ).all()
            if not models:
                print(f"未找到 {args.platform} 账号：{email}")
            for model in models:
                print(reset_account(session, model, stage=args.stage, apply=args.apply))

    if not args.apply:
        print("只打印，未写库；确认无误后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
