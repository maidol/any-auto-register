"""需求 20260926 第 2 条：账号列表里有注册失败的账号，就复用它的邮箱去注册。

- 失败阶段是「已在 ChatGPT 建过号」（created_oauth_failed / created_other_failed）
  → 同一对邮箱/密码直接续做 Codex OAuth；
- 其他（not_created / unknown）→ 同一对邮箱/密码走完整注册；
- 只复用邮箱 provider 读得到收件箱的那些（今天只有 CF Worker，而且要是它配置的域名）；
- 同一条失败账号同一时间只给一个周期用，同一个任务里只用一次，累计最多复用 3 次。
"""
from __future__ import annotations

from sqlmodel import Session, select

import application.tasks as tasks_module
from core import db as db_module
from core.base_mailbox import CFWorkerMailbox, MailboxAccount, PinnedMailbox
from core.base_platform import Account


def _overview(email: str, platform: str = "chatgpt") -> dict:
    from core.account_graph import load_account_graphs

    with Session(db_module.engine) as session:
        model = session.exec(
            select(db_module.AccountModel)
            .where(db_module.AccountModel.platform == platform)
            .where(db_module.AccountModel.email == email)
        ).first()
        graph = load_account_graphs(session, [int(model.id)]).get(int(model.id), {})
    return {**(graph.get("overview") or {}), "lifecycle_status": graph.get("lifecycle_status")}


def _failed(email: str, stage: str, *, password: str = "OldPw123!", mail_provider: str = "") -> None:
    # mail_provider 只在给了的时候才传：这样在没打补丁的树上，其余测试红的是行为，不是参数名。
    kwargs = {"mail_provider": mail_provider} if mail_provider else {}
    db_module.save_failed_account(
        "chatgpt", email, password,
        failure_stage=stage, failure_reason="earlier failure", **kwargs,
    )


# ══════════════════════════════════════════════════════════════════════
# 存储层
# ══════════════════════════════════════════════════════════════════════

def test_a_failed_row_records_its_mail_provider():
    _failed("a@reuse.test", "not_created", mail_provider="cfworker_admin_api")
    assert _overview("a@reuse.test")["mail_provider"] == "cfworker_admin_api"


def test_reusable_rows_are_failed_ones_oldest_first():
    _failed("first@reuse.test", "not_created")
    db_module.save_account(Account(platform="chatgpt", email="good@reuse.test", password="Good123!"))
    _failed("second@reuse.test", "created_oauth_failed")
    _failed("nopw@reuse.test", "not_created", password="")

    rows = db_module.list_reusable_failed_accounts("chatgpt")

    assert [r["email"] for r in rows] == ["first@reuse.test", "second@reuse.test"]
    assert rows[1] == {
        "email": "second@reuse.test", "password": "OldPw123!",
        "failure_stage": "created_oauth_failed", "mail_provider": "",
    }


def test_a_row_stops_being_reusable_after_the_cap():
    _failed("cap@reuse.test", "not_created")
    for _ in range(db_module.FAILED_ACCOUNT_REUSE_MAX):
        db_module.mark_failed_account_reused("chatgpt", "cap@reuse.test")

    assert _overview("cap@reuse.test")["reuse_attempts"] == db_module.FAILED_ACCOUNT_REUSE_MAX
    assert db_module.list_reusable_failed_accounts("chatgpt") == []


def test_marking_leaves_the_failure_markers_and_password_alone():
    _failed("keep@reuse.test", "created_other_failed", mail_provider="cfworker_admin_api")
    db_module.mark_failed_account_reused("chatgpt", "keep@reuse.test")

    overview = _overview("keep@reuse.test")
    assert overview["lifecycle_status"] == "failed"
    assert overview["failure_stage"] == "created_other_failed"
    assert overview["mail_provider"] == "cfworker_admin_api"
    assert db_module.list_reusable_failed_accounts("chatgpt")[0]["password"] == "OldPw123!"


def test_success_clears_the_reuse_markers():
    _failed("ok@reuse.test", "not_created", mail_provider="cfworker_admin_api")
    db_module.mark_failed_account_reused("chatgpt", "ok@reuse.test")
    db_module.save_account(Account(platform="chatgpt", email="ok@reuse.test", password="OldPw123!"))

    overview = _overview("ok@reuse.test")
    assert overview["lifecycle_status"] == "registered"
    assert "mail_provider" not in overview and "reuse_attempts" not in overview


# ══════════════════════════════════════════════════════════════════════
# 邮箱层
# ══════════════════════════════════════════════════════════════════════

class _CountingMailbox:
    def __init__(self):
        self.opened = 0

    def get_email(self):
        self.opened += 1
        return MailboxAccount(email=f"new{self.opened}@reuse.test")


def test_a_pinned_mailbox_can_start_on_an_existing_address():
    wrapped = _CountingMailbox()
    mailbox = PinnedMailbox(wrapped, pinned=MailboxAccount(email="old@reuse.test"))

    assert mailbox.get_email().email == "old@reuse.test"
    assert mailbox.pinned_email == "old@reuse.test"
    assert wrapped.opened == 0, "复用老邮箱时不许再开新地址"


def test_cfworker_reads_only_addresses_on_its_own_domain():
    mailbox = CFWorkerMailbox(api_url="http://worker.invalid", domain="drxmai.com")
    assert mailbox.can_read_address("abc@drxmai.com") is True
    assert mailbox.can_read_address("ABC@DRXMAI.COM") is True
    assert mailbox.can_read_address("abc@other.com") is False
    assert mailbox.can_read_address("abc@evil-drxmai.com") is False
    assert CFWorkerMailbox(api_url="http://worker.invalid").can_read_address("abc@drxmai.com") is False


# ══════════════════════════════════════════════════════════════════════
# 任务层
# ══════════════════════════════════════════════════════════════════════

def _world(monkeypatch, **kw):
    from tests.test_cycle_credentials_and_resume import world

    w = world(monkeypatch, **kw)
    # 能按地址读 @reuse.test 的邮箱，模拟 CF Worker 配了这个域名。
    w.mailbox.can_read_address = lambda email: email.endswith("@reuse.test")
    # 用真的 save_account：成功之后那条失败行要真的变成已注册。
    monkeypatch.setattr("application.tasks.save_account", db_module.save_account)
    return w


def _resume_stage(attempt: dict) -> str:
    return str((attempt["extra"].get("registration_resume") or {}).get("stage") or "")


def test_a_created_failed_account_is_reused_and_goes_straight_to_oauth(monkeypatch):
    _failed("old@reuse.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0, executor_type="headless")

    assert w.emails == ["old@reuse.test"]
    assert w.passwords == ["OldPw123!"], "复用必须带着原来的密码，账号是用它建的"
    assert _resume_stage(w.attempts[0]) == "account_created"
    assert w.mailbox.n == 0, "复用老邮箱时不许再开新地址"
    assert _overview("old@reuse.test")["lifecycle_status"] == "registered"


def test_created_other_failed_also_goes_to_oauth(monkeypatch):
    _failed("half@reuse.test", "created_other_failed")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0, executor_type="headless")

    assert w.emails == ["half@reuse.test"]
    assert _resume_stage(w.attempts[0]) == "account_created"


def test_a_not_created_failed_account_runs_the_full_signup(monkeypatch):
    _failed("never@reuse.test", "not_created")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0)

    assert w.emails == ["never@reuse.test"]
    assert w.passwords == ["OldPw123!"]
    assert _resume_stage(w.attempts[0]) == ""


def test_no_reuse_when_the_mailbox_cannot_read_that_address(monkeypatch):
    _failed("old@elsewhere.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0, executor_type="headless")

    assert w.emails == ["box1@example.com"]


def test_no_reuse_of_a_row_made_by_another_mail_provider(monkeypatch):
    _failed("old@reuse.test", "created_oauth_failed", mail_provider="cfworker_admin_api")
    w = _world(monkeypatch, outcomes=["ok"])  # 当前 provider 是 moemail

    w.run(count=1, retry_count=0, executor_type="headless")

    assert w.emails == ["box1@example.com"]


def test_no_reuse_when_the_task_names_its_own_email(monkeypatch):
    _failed("old@reuse.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["ok"])

    w.run(count=1, retry_count=0, email="fixed@example.com", executor_type="headless")

    assert w.passwords != ["OldPw123!"]
    assert _resume_stage(w.attempts[0]) == ""


def test_failed_rows_are_used_up_before_new_mailboxes(monkeypatch):
    _failed("one@reuse.test", "not_created")
    _failed("two@reuse.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["ok", "ok", "ok"])

    w.run(count=3, retry_count=0, executor_type="headless")

    assert w.emails == ["one@reuse.test", "two@reuse.test", "box1@example.com"]


def test_a_task_does_not_retry_the_same_failed_row_in_a_second_cycle(monkeypatch):
    """复用又失败了：同一个任务的下一轮不许再挑它，否则一个坏邮箱会连吃三轮。"""
    _failed("bad@reuse.test", "not_created")
    w = _world(monkeypatch, outcomes=["boom", "ok"])

    w.run(count=1, retry_count=0, max_attempts=2)

    assert w.emails == ["bad@reuse.test", "box1@example.com"]
    overview = _overview("bad@reuse.test")
    assert overview["lifecycle_status"] == "failed"
    assert overview["reuse_attempts"] == 1


def test_a_failed_reuse_keeps_the_mail_provider_on_the_row(monkeypatch):
    _failed("bad@reuse.test", "not_created")
    w = _world(monkeypatch, outcomes=["boom"])

    w.run(count=1, retry_count=0, max_attempts=1)

    assert _overview("bad@reuse.test")["mail_provider"] == "moemail"


def test_the_claim_is_released_when_the_task_ends(monkeypatch):
    _failed("old@reuse.test", "created_oauth_failed")
    w = _world(monkeypatch, outcomes=["boom"])

    w.run(count=1, retry_count=0, max_attempts=1, executor_type="headless")

    assert tasks_module._reused_failed_accounts == set()


def test_two_concurrent_cycles_never_get_the_same_row():
    _failed("one@reuse.test", "not_created")
    _failed("two@reuse.test", "not_created")
    mailbox = _CountingMailbox()
    mailbox.can_read_address = lambda email: True
    try:
        first = tasks_module._claim_failed_account("chatgpt", mailbox, "", set())
        second = tasks_module._claim_failed_account("chatgpt", mailbox, "", set())
        third = tasks_module._claim_failed_account("chatgpt", mailbox, "", set())
    finally:
        tasks_module._reused_failed_accounts.clear()

    assert [first["email"], second["email"]] == ["one@reuse.test", "two@reuse.test"]
    assert third is None
