from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core import db as core_db
from core.db import AccountModel, AccountOverviewModel, save_failed_account
from domain.accounts import AccountCreateCommand
from infrastructure.accounts_repository import AccountsRepository
from tools.reset_account_reuse import main


def _create_failed_row(email: str, attempts: int = 3, stage: str = "created_oauth_failed") -> int:
    save_failed_account("chatgpt", email, "pwd123", failure_stage=stage, failure_reason="oauth err")
    with Session(core_db.engine) as session:
        model = session.exec(select(AccountModel).where(AccountModel.email == email)).first()
        overview = session.exec(select(AccountOverviewModel).where(AccountOverviewModel.account_id == model.id)).first()
        summary = overview.get_summary()
        summary["reuse_attempts"] = attempts
        overview.set_summary(summary)
        session.add(overview)
        session.commit()
        return int(model.id)


def _create_manual_invalid_row(email: str, attempts: int = 3) -> int:
    repo = AccountsRepository()
    acc = repo.create(
        AccountCreateCommand(
            platform="chatgpt",
            email=email,
            password="pwd",
            lifecycle_status="registered",
            overview={"account_origin": "manual", "valid": False, "manual_reuse_attempts": attempts},
        )
    )
    return acc.id


def test_r1_dry_run_does_not_modify_database(capsys):
    """R1: 默认只打印、不写库，加 --apply 才写。最后一行写清是不是试运行。"""
    acc_id = _create_failed_row("r1@example.com", attempts=3)
    code = main(["r1@example.com"])
    assert code == 0
    out = capsys.readouterr().out
    assert "只打印，未写库；确认无误后加 --apply" in out
    assert "将重置" in out
    record = AccountsRepository().get(acc_id)
    assert record.overview.get("reuse_attempts") == 3


def test_r2_failed_row_resets_reuse_attempts():
    """R2: 注册失败行（lifecycle_status == 'failed'）：reuse_attempts 清零。"""
    acc_id = _create_failed_row("r2@example.com", attempts=3)
    code = main(["--apply", "r2@example.com"])
    assert code == 0
    record = AccountsRepository().get(acc_id)
    assert record.overview.get("reuse_attempts") == 0


def test_r3_manual_invalid_row_resets_manual_reuse_attempts():
    """R3: 手动无效行：清零 manual_reuse_attempts。"""
    acc_id = _create_manual_invalid_row("r3@example.com", attempts=3)
    code = main(["--apply", "r3@example.com"])
    assert code == 0
    record = AccountsRepository().get(acc_id)
    assert record.overview.get("manual_reuse_attempts") == 0


def test_r4_other_rows_are_skipped(capsys):
    """R4: 其他行一律跳过并打印原因，包括 registered、disabled 等。"""
    repo = AccountsRepository()
    # 正常注册账号
    reg_acc = repo.create(AccountCreateCommand(platform="chatgpt", email="reg@example.com", password="pwd"))
    # disabled 账号
    dis_id = _create_failed_row("dis@example.com")
    repo.set_disabled(dis_id, disabled=True)

    code = main(["--apply", "reg@example.com", "dis@example.com"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"跳过 #{reg_acc.id} reg@example.com [registered]：不是注册失败或手动添加的无效账号" in out
    assert f"跳过 #{dis_id} dis@example.com [disabled]：已停用账号不重置" in out


def test_r5_stage_option_only_affects_failed_rows(capsys):
    """R5: 可选 --stage 只能用于失败行，更新失败阶段并打印旧值到新值；用于手动无效行跳过并打印原因。"""
    failed_id = _create_failed_row("stage_failed@example.com", stage="not_created")
    manual_id = _create_manual_invalid_row("stage_manual@example.com")

    code = main(["--stage", "created_oauth_failed", "--apply", "stage_failed@example.com", "stage_manual@example.com"])
    assert code == 0
    out = capsys.readouterr().out
    assert "not_created → created_oauth_failed" in out
    assert "跳过 stage 指定" in out or "仅失败行可指定" in out

    failed_rec = AccountsRepository().get(failed_id)
    assert failed_rec.overview.get("failure_stage") == "created_oauth_failed"


def test_r6_script_does_not_modify_lifecycle_or_valid():
    """R6: 脚本不改 lifecycle_status、valid、account_origin、plan_state。"""
    manual_id = _create_manual_invalid_row("r6@example.com")
    main(["--apply", "r6@example.com"])
    record = AccountsRepository().get(manual_id)
    assert record.lifecycle_status == "registered"
    assert record.validity_status == "invalid"
    assert record.overview.get("account_origin") == "manual"


def test_r7_processes_multiple_rows_for_same_email(capsys):
    """R7: 同一个邮箱有几行就处理几行；查不到的邮箱打印未找到。"""
    id1 = _create_failed_row("dup@example.com")
    # 创建同邮箱第二行（通过直接插入 AccountModel 并关联失败 overview）
    with Session(core_db.engine) as session:
        m2 = AccountModel(platform="chatgpt", email="dup@example.com", password="pwd")
        session.add(m2)
        session.commit()
        session.refresh(m2)
        id2 = int(m2.id)
        ov2 = AccountOverviewModel(account_id=id2, lifecycle_status="failed")
        ov2.set_summary({"lifecycle_status": "failed", "reuse_attempts": 3})
        session.add(ov2)
        session.commit()

    code = main(["--apply", "dup@example.com", "missing@example.com"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"#{id1} dup@example.com" in out
    assert f"#{id2} dup@example.com" in out
    assert "未找到 chatgpt 账号：missing@example.com" in out
    rec1 = AccountsRepository().get(id1)
    rec2 = AccountsRepository().get(id2)
    assert rec1.overview.get("reuse_attempts") == 0
    assert rec2.overview.get("reuse_attempts") == 0


def test_r9_output_formatting(capsys):
    """R9: 区分跳过、将重置、已重置，带 #id、email 和生命周期。"""
    failed_id = _create_failed_row("fmt@example.com")
    main(["fmt@example.com"])
    out = capsys.readouterr().out
    assert f"将重置 #{failed_id} fmt@example.com [failed]" in out

    main(["--apply", "fmt@example.com"])
    out2 = capsys.readouterr().out
    assert f"已重置 #{failed_id} fmt@example.com [failed]" in out2


def test_r3_non_manual_invalid_row_is_skipped(capsys):
    """R3: 无效但不是手动添加的行跳过，计数不动。"""
    acc = AccountsRepository().create(
        AccountCreateCommand(
            platform="chatgpt",
            email="r3_auto@example.com",
            password="pwd",
            lifecycle_status="registered",
            overview={"valid": False, "manual_reuse_attempts": 3},
        )
    )
    main(["--apply", "r3_auto@example.com"])
    out = capsys.readouterr().out
    assert f"跳过 #{acc.id} r3_auto@example.com [registered]：不是注册失败或手动添加的无效账号" in out
    assert AccountsRepository().get(acc.id).overview.get("manual_reuse_attempts") == 3


def test_r3_manual_valid_row_is_skipped(capsys):
    """R3: 手动添加但有效的行跳过，计数不动。"""
    acc = AccountsRepository().create(
        AccountCreateCommand(
            platform="chatgpt",
            email="r3_valid@example.com",
            password="pwd",
            lifecycle_status="registered",
            overview={"account_origin": "manual", "valid": True, "manual_reuse_attempts": 3},
        )
    )
    main(["--apply", "r3_valid@example.com"])
    out = capsys.readouterr().out
    assert f"跳过 #{acc.id} r3_valid@example.com [registered]：不是注册失败或手动添加的无效账号" in out
    assert AccountsRepository().get(acc.id).overview.get("manual_reuse_attempts") == 3


def test_r3_manual_invalid_row_without_password_is_skipped(capsys):
    """R3: 手动无效但没有密码的行跳过，计数不动。"""
    acc = AccountsRepository().create(
        AccountCreateCommand(
            platform="chatgpt",
            email="r3_nopwd@example.com",
            password="",
            lifecycle_status="registered",
            overview={"account_origin": "manual", "valid": False, "manual_reuse_attempts": 3},
        )
    )
    main(["--apply", "r3_nopwd@example.com"])
    out = capsys.readouterr().out
    assert f"跳过 #{acc.id} r3_nopwd@example.com [registered]：账号无密码无法复用" in out
    assert AccountsRepository().get(acc.id).overview.get("manual_reuse_attempts") == 3
