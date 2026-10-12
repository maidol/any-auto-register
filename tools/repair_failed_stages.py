"""运维脚本：补回注册失败账号丢失的失败阶段（failure_stage）。

65f02e4 之前，注册失败的账号在停用时会被清掉 failure_stage / failure_reason，
恢复启用后它回到「注册失败」，但列表里不再显示「已建号·OAuth失败」这类细分，
复用时也会被当成「未新建」重走完整注册。

只处理当前是「注册失败」、或停用前是「注册失败」、且缺 failure_stage 的账号；
已有 failure_stage 的不覆盖，生命周期不改。不加 --apply 只打印、不写库。
写库前先备份数据库文件，例如：cp data/account_manager.db data/account_manager.db.bak

用法（数据库位置跟随 ACCOUNT_MANAGER_DATABASE_URL，与服务相同）：
  .venv311/bin/python tools/repair_failed_stages.py --stage created_oauth_failed a@example.com b@example.com
  .venv311/bin/python tools/repair_failed_stages.py --stage created_oauth_failed --apply a@example.com b@example.com
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


def repair_account(session: Session, model: AccountModel, *, stage: str, apply: bool) -> str:
    """处理一行账号，返回一行说明。"""
    graph = _graph_for_account(session, int(model.id))
    overview = graph.get("overview") or {}
    lifecycle = str(graph.get("lifecycle_status") or "")
    label = f"#{model.id} {model.email} [{lifecycle}]"
    if lifecycle not in ("failed", "disabled"):
        return f"跳过 {label}：不是注册失败账号"
    if lifecycle == "disabled" and overview.get("previous_lifecycle_status") != "failed":
        return f"跳过 {label}：停用前不是注册失败"
    if overview.get("failure_stage"):
        return f"跳过 {label}：已有 failure_stage={overview.get('failure_stage')}"
    if not apply:
        return f"将补 {label}：failure_stage={stage}"
    patch_account_graph(session, model, summary_updates={"failure_stage": stage})
    session.commit()
    return f"已补 {label}：failure_stage={stage}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补回注册失败账号丢失的 failure_stage")
    parser.add_argument("--stage", required=True, choices=STAGES, help="要补回的失败阶段")
    parser.add_argument("--platform", default="chatgpt", help="平台，默认 chatgpt")
    parser.add_argument("--apply", action="store_true", help="真的写库；不加只打印")
    parser.add_argument("emails", nargs="+", help="要处理的邮箱")
    args = parser.parse_args(argv)

    with Session(core_db.engine) as session:
        for raw_email in args.emails:
            email = raw_email.strip()
            models = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == args.platform)
                .where(AccountModel.email == email)
            ).all()
            if not models:
                print(f"未找到 {args.platform} 账号：{email}")
            for model in models:
                print(repair_account(session, model, stage=args.stage, apply=args.apply))
    if not args.apply:
        print("只打印，未写库；确认无误后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
