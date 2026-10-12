from __future__ import annotations

from sqlmodel import Session, select

from core import db as core_db
from core.db import AccountModel, AccountOverviewModel, save_failed_account
from domain.accounts import AccountCreateCommand
from infrastructure.accounts_repository import AccountsRepository
from tools.repair_failed_stages import main


def _drop_stage(account_id: int) -> None:
    """造出 65f02e4 之前的受损数据：账号的 failure_stage 被清掉了。"""
    with Session(core_db.engine) as session:
        overview = session.exec(
            select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)
        ).first()
        summary = overview.get_summary()
        summary.pop("failure_stage", None)
        overview.set_summary(summary)
        session.add(overview)
        session.commit()


def _failed_account(email: str) -> int:
    save_failed_account("chatgpt", email, "pwd", failure_stage="created_oauth_failed", failure_reason="oauth boom")
    with Session(core_db.engine) as session:
        return int(session.exec(select(AccountModel).where(AccountModel.email == email)).first().id)


def test_dry_run_does_not_write(capsys):
    account_id = _failed_account("dry@example.com")
    _drop_stage(account_id)
    assert main(["--stage", "created_oauth_failed", "dry@example.com"]) == 0
    assert "将补" in capsys.readouterr().out
    assert not AccountsRepository().get(account_id).overview.get("failure_stage")


def test_apply_fills_missing_stage_and_keeps_lifecycle():
    account_id = _failed_account("fill@example.com")
    _drop_stage(account_id)
    main(["--stage", "created_oauth_failed", "--apply", "fill@example.com"])
    record = AccountsRepository().get(account_id)
    assert record.lifecycle_status == "failed"
    assert record.overview.get("failure_stage") == "created_oauth_failed"
    assert record.overview.get("failure_reason") == "oauth boom"


def test_existing_stage_is_not_overwritten():
    account_id = _failed_account("keep@example.com")
    main(["--stage", "not_created", "--apply", "keep@example.com"])
    assert AccountsRepository().get(account_id).overview.get("failure_stage") == "created_oauth_failed"


def test_registered_account_is_untouched(capsys):
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(platform="chatgpt", email="ok@example.com", password="pwd"))
    main(["--stage", "created_oauth_failed", "--apply", "ok@example.com"])
    assert "跳过" in capsys.readouterr().out
    record = repo.get(acc.id)
    assert record.lifecycle_status == "registered"
    assert not record.overview.get("failure_stage")


def test_disabled_account_that_was_failed_is_filled_and_stays_disabled():
    account_id = _failed_account("frozen@example.com")
    repo = AccountsRepository()
    repo.set_disabled(account_id, disabled=True)
    _drop_stage(account_id)
    main(["--stage", "created_oauth_failed", "--apply", "frozen@example.com"])
    record = repo.get(account_id)
    assert record.lifecycle_status == "disabled"
    assert record.overview.get("failure_stage") == "created_oauth_failed"


def test_disabled_account_that_was_registered_is_untouched():
    repo = AccountsRepository()
    acc = repo.create(AccountCreateCommand(platform="chatgpt", email="paused@example.com", password="pwd"))
    repo.set_disabled(acc.id, disabled=True)
    main(["--stage", "created_oauth_failed", "--apply", "paused@example.com"])
    record = repo.get(acc.id)
    assert record.lifecycle_status == "disabled"
    assert not record.overview.get("failure_stage")
