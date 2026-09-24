"""注册失败的账号也要留在账号列表里，并标明卡在哪一段。

以前只有成功才落库：账号已经在 ChatGPT 侧建好、只差 Codex OAuth 时，
这对邮箱/密码只出现在任务日志里，账号列表看不见，变成孤儿账号。

细分阶段写在 overview.failure_stage：
  not_created           ChatGPT 侧还没有这个账号（注册密码还没被接受）
  created_other_failed  注册密码已被接受（或落到了老账号登录页），之后某一步失败
  created_oauth_failed  注册状态机走完了，Codex OAuth 拿不到完整 token
  unknown               抛的是通用异常，分不出阶段（其他平台、协议模式）

failure_stage 只管展示。决定下一次尝试要不要跳过注册入口的仍然是
RegistrationAttemptError.stage，这两件事不许合并。
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core import db as db_module
from core.base_platform import Account, AccountStatus


def _row(email: str, platform: str = "chatgpt"):
    from core.account_graph import load_account_graphs

    with Session(db_module.engine) as session:
        model = session.exec(
            select(db_module.AccountModel)
            .where(db_module.AccountModel.platform == platform)
            .where(db_module.AccountModel.email == email)
        ).first()
        if model is None:
            return None, {}
        graph = load_account_graphs(session, [int(model.id)]).get(int(model.id), {})
        return model, graph


# ══════════════════════════════════════════════════════════════════════
# 存储层
# ══════════════════════════════════════════════════════════════════════

def test_failed_account_is_saved_with_stage_and_reason():
    from core.db import save_failed_account

    written = save_failed_account(
        "chatgpt", "f@example.com", "Pw123456!",
        failure_stage="created_oauth_failed", failure_reason="oauth boom",
    )

    model, graph = _row("f@example.com")
    assert written is True
    assert model is not None, "注册失败的账号没有落库"
    assert model.password == "Pw123456!", "失败账号必须带着这一轮的密码，否则救不回来"
    assert graph["lifecycle_status"] == "failed"
    assert graph["display_status"] == "failed"
    assert graph["overview"]["failure_stage"] == "created_oauth_failed"
    assert graph["overview"]["failure_reason"] == "oauth boom"
    assert graph["overview"]["failed_at"]


def test_a_failed_attempt_never_downgrades_a_good_account():
    """固定邮箱重跑时，一次失败的尝试不许把好账号改成「注册失败」、更不许换掉密码。"""
    from core.db import save_account, save_failed_account

    save_account(Account(platform="chatgpt", email="g@example.com", password="Good123!"))

    written = save_failed_account(
        "chatgpt", "g@example.com", "Other123!",
        failure_stage="not_created", failure_reason="boom",
    )

    model, graph = _row("g@example.com")
    assert written is False
    assert model.password == "Good123!", "失败的那次尝试换掉了好账号的密码"
    assert graph["lifecycle_status"] == "registered"
    assert "failure_stage" not in graph["overview"]


def test_success_after_failure_clears_the_failure_markers():
    """overview 是合并写的：成功那次不显式删掉，失败阶段会留在一个已注册的账号上。"""
    from core.db import save_account, save_failed_account

    save_failed_account(
        "chatgpt", "r@example.com", "Pw123456!",
        failure_stage="created_oauth_failed", failure_reason="oauth boom",
    )
    save_account(Account(platform="chatgpt", email="r@example.com", password="Pw123456!"))

    _model, graph = _row("r@example.com")
    assert graph["lifecycle_status"] == "registered"
    leftovers = sorted(k for k in ("failure_stage", "failure_reason", "failed_at") if k in graph["overview"])
    assert leftovers == [], f"注册成功后 overview 里还留着 {leftovers}"


def test_select_all_export_leaves_failed_rows_out():
    """全选导出/推送不带出失败账号；显式筛选或显式勾选时照常带出。"""
    from core.db import save_account, save_failed_account
    from domain.accounts import AccountExportSelection
    from infrastructure.accounts_repository import AccountsRepository

    save_account(Account(platform="chatgpt", email="ok@example.com", password="Good123!"))
    save_failed_account(
        "chatgpt", "bad@example.com", "Pw123456!",
        failure_stage="not_created", failure_reason="boom",
    )
    failed_model, _graph = _row("bad@example.com")
    repo = AccountsRepository()

    everything = repo.select_for_export(AccountExportSelection(platform="chatgpt", select_all=True))
    only_failed = repo.select_for_export(
        AccountExportSelection(platform="chatgpt", select_all=True, status_filter="failed")
    )
    ticked = repo.select_for_export(AccountExportSelection(platform="chatgpt", ids=[int(failed_model.id)]))

    assert [r.email for r in everything] == ["ok@example.com"], (
        "全选导出带出了注册失败的账号：它们没有 token，混进 Sub2API/CPA 就是一批空凭据"
    )
    assert [r.email for r in only_failed] == ["bad@example.com"]
    assert [r.email for r in ticked] == ["bad@example.com"]


# ══════════════════════════════════════════════════════════════════════
# 执行器：ChatGPTBrowserRegister.run 给出细分阶段
# ══════════════════════════════════════════════════════════════════════

class _FakeBrowser:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def new_page(self):
        return object()


def _worker(monkeypatch, flow, oauth_result=None):
    import platforms.chatgpt.browser_register as br

    monkeypatch.setattr(br, "_browser_registration_flow", flow)
    monkeypatch.setattr(br, "_get_cookies", lambda page: {})
    monkeypatch.setattr(br, "Camoufox", lambda **kw: _FakeBrowser())
    monkeypatch.setattr(
        br.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser",
        lambda self, email, password: oauth_result,
    )
    return br.ChatGPTBrowserRegister(headless=True, log_fn=lambda *_a, **_k: None)


def test_state_machine_failure_before_the_password_is_not_created(monkeypatch):
    from core.registration.errors import RegistrationAttemptError

    def _flow(*a, **k):
        raise RuntimeError("未获取到验证码")

    worker = _worker(monkeypatch, _flow)
    with pytest.raises(RegistrationAttemptError) as err:
        worker.run(email="u@example.com", password="Pinned123!")

    assert err.value.failure_stage == "not_created"
    assert err.value.stage == "signup_started"


def test_state_machine_failure_after_the_password_is_created_other_failed(monkeypatch):
    from core.registration.errors import RegistrationAttemptError

    def _flow(*a, progress=None, **k):
        progress["account_exists"] = True
        raise RuntimeError("手机号验证失败")

    worker = _worker(monkeypatch, _flow)
    with pytest.raises(RegistrationAttemptError) as err:
        worker.run(email="u@example.com", password="Pinned123!")

    assert err.value.failure_stage == "created_other_failed"
    # 恢复语义不许跟着变：状态机没走完，下一次尝试仍然要从注册入口进。
    assert err.value.stage == "signup_started"
    assert err.value.account_created is False


def test_oauth_failure_is_created_oauth_failed(monkeypatch):
    from core.registration.errors import RegistrationAttemptError

    worker = _worker(monkeypatch, lambda *a, **k: {"page_type": "chatgpt_home"})
    with pytest.raises(RegistrationAttemptError) as err:
        worker.run(email="u@example.com", password="Pinned123!")

    assert err.value.failure_stage == "created_oauth_failed"
    assert err.value.stage == "account_created"


# ══════════════════════════════════════════════════════════════════════
# 状态机本身：account_exists 是真的在密码被接受之后才置上的
# （上面几条把状态机整个替换掉了，接线要靠这几条钉）
# ══════════════════════════════════════════════════════════════════════

def _stub_state_machine(monkeypatch, *, start_page, password_ok=True, login_ok=False):
    import platforms.chatgpt.browser_register as br

    class _Page:
        url = "https://auth.openai.com/x"

        def evaluate(self, _js):
            return "UA"

    monkeypatch.setattr(br, "_seed_browser_device_id", lambda page, device_id: None)
    monkeypatch.setattr(br, "_start_browser_signup_via_page", lambda page, email, log: {"page_type": start_page})
    monkeypatch.setattr(br, "_get_cookies", lambda page: {})
    monkeypatch.setattr(br, "_is_registration_complete", lambda s: False)
    monkeypatch.setattr(br, "_is_password_registration", lambda s: s.get("page_type") == "create_account_password")
    monkeypatch.setattr(br, "_is_email_otp", lambda s: False)
    monkeypatch.setattr(br, "_is_about_you", lambda s: False)
    monkeypatch.setattr(br, "_is_add_phone", lambda s: False)
    monkeypatch.setattr(br, "_requires_registration_navigation", lambda s: False)
    monkeypatch.setattr(br, "_recover_signup_password_page", lambda page, log: False)
    monkeypatch.setattr(br, "_extract_flow_state", lambda data, url: {"page_type": "somewhere_unknown"})
    monkeypatch.setattr(br, "_derive_registration_state_from_page", lambda page: {"page_type": "somewhere_unknown"})
    monkeypatch.setattr(
        br, "_submit_password_via_page",
        lambda page, password, log: {"ok": password_ok, "status": 200 if password_ok else 400, "text": "x"},
    )
    monkeypatch.setattr(
        br, "_submit_oauth_password_direct",
        lambda page, password, log: {"ok": login_ok, "status": 200 if login_ok else 401, "text": "x"},
    )
    return br, _Page()


def test_the_real_flow_marks_the_account_after_the_password_is_accepted(monkeypatch):
    br, page = _stub_state_machine(monkeypatch, start_page="create_account_password", password_ok=True)
    progress: dict = {}

    with pytest.raises(RuntimeError, match="未支持的注册状态"):
        br._browser_registration_flow(page, "u@example.com", "Pw123456!", None, None, lambda *_a: None, progress=progress)

    assert progress.get("account_exists") is True


def test_the_real_flow_leaves_the_mark_off_when_the_password_is_rejected(monkeypatch):
    br, page = _stub_state_machine(monkeypatch, start_page="create_account_password", password_ok=False)
    progress: dict = {}

    with pytest.raises(RuntimeError, match="密码页提交失败"):
        br._browser_registration_flow(page, "u@example.com", "Pw123456!", None, None, lambda *_a: None, progress=progress)

    assert progress.get("account_exists") is not True


def test_the_real_flow_marks_the_account_when_it_lands_on_the_login_page(monkeypatch):
    """落到「已有账号登录密码页」说明 OpenAI 那边已经有这个邮箱了。"""
    br, page = _stub_state_machine(monkeypatch, start_page="login_password", login_ok=False)
    progress: dict = {}

    with pytest.raises(RuntimeError, match="登录密码页提交失败"):
        br._browser_registration_flow(page, "u@example.com", "Pw123456!", None, None, lambda *_a: None, progress=progress)

    assert progress.get("account_exists") is True


# ══════════════════════════════════════════════════════════════════════
# 任务层：每次失败的尝试都落一行，写库失败不改变结果
# ══════════════════════════════════════════════════════════════════════

def test_a_failed_cycle_leaves_a_failed_account_row(monkeypatch):
    from tests.test_cycle_credentials_and_resume import world

    w = world(monkeypatch, outcomes=[], default_outcome="boom")
    w.run(count=1, retry_count=1, max_attempts=2)

    model, graph = _row("box1@example.com")
    assert model is not None, "整轮失败之后账号列表里没有这个邮箱"
    assert model.password == w.passwords[0]
    assert graph["lifecycle_status"] == "failed"
    assert graph["overview"]["failure_stage"] == "unknown", "通用异常分不出阶段，就不许冒充 not_created"
    assert graph["overview"]["failure_reason"] == "boom"


def test_oauth_failure_row_carries_the_stage_from_the_executor(monkeypatch):
    from core.registration.errors import RegistrationAttemptError
    from tests.test_cycle_credentials_and_resume import world

    w = world(monkeypatch, outcomes=[], default_outcome="boom")

    def _oauth_failed(platform):
        raise RegistrationAttemptError(
            "oauth failed",
            stage="account_created",
            failure_stage="created_oauth_failed",
            email=w.attempts[-1]["email"],
            password=w.attempts[-1]["password"],
        )

    w.on_attempt = [_oauth_failed]
    w.run(count=1, retry_count=0, max_attempts=1)

    model, graph = _row("box1@example.com")
    assert model is not None
    assert model.password == w.passwords[0]
    assert graph["overview"]["failure_stage"] == "created_oauth_failed"


def _explode(*a, **k):
    raise RuntimeError("database is locked")


def test_a_broken_failed_account_save_does_not_stop_the_retry(monkeypatch):
    from tests.test_cycle_credentials_and_resume import world

    w = world(monkeypatch, outcomes=["boom", "ok"])
    monkeypatch.setattr("application.tasks.save_failed_account", _explode)
    task = w.run(count=1, retry_count=1, max_attempts=2)

    assert len(w.attempts) == 2, "失败账号写库失败打断了重试"
    assert len(w.saved) == 1, "写库失败之后第 2 次尝试的成功没有落库"
    assert task["status"] == "succeeded"


def test_a_broken_failed_account_save_does_not_replace_the_real_error(monkeypatch):
    """调度内核本来就会接住 register_once 抛出的异常，所以上一条摘掉 try 也是绿的。
    摘掉 try 真正坏掉的是这里：写库异常顶替了这次尝试真正的失败原因。"""
    from tests.test_cycle_credentials_and_resume import world

    w = world(monkeypatch, outcomes=[], default_outcome="boom")
    monkeypatch.setattr("application.tasks.save_failed_account", _explode)
    task = w.run(count=1, retry_count=1, max_attempts=2)

    assert len(w.attempts) == 2
    assert task["status"] == "failed"
    assert task["error"] == "boom", f"任务报的失败原因是 {task['error']!r}，不是这次尝试真正的错误"
