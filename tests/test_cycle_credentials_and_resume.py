"""生产故障 task_1790001997941_b16493：一轮之内第二次尝试「逻辑不适用」。

Phase 4 把**邮箱**钉到了周期级，密码没有跟着钉：`BasePlatform.register()`
在 payload 没给密码时每次尝试都现生成一个随机密码，于是第 1 次尝试用 P1 建好的
账号，第 2 次尝试拿着 P2 去登录。凭据对只钉住了一半。

第二件事：第 1 次尝试「账号已建好、OAuth 失败」时抛的是通用异常，
`_execute_register_task` 只能记成 account_saved=False，于是第 2 次从注册入口
整段重放 —— 而那个邮箱在 OpenAI 那边已经是老账号了。

第三件事：注册状态机的邮箱验证码分支按 `continue_url` 判定却在**当前页面**打字，
浏览器还没跳过去。现象就是生产日志里那句「验证码页未找到可填写输入框」，
而验证码在报错**之前**已经被消费掉了。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.tasks import (
    TaskLogger,
    _execute_register_task,
    create_task,
    get_task,
)
from core.base_mailbox import BaseMailbox, MailboxAccount


# ══════════════════════════════════════════════════════════════════════
# 任务层：一轮 = 一对凭据
# ══════════════════════════════════════════════════════════════════════

class FakeMailbox(BaseMailbox):
    def __init__(self) -> None:
        self.n = 0
        self.messages: dict[str, list[str]] = {}

    def get_email(self) -> MailboxAccount:
        self.n += 1
        addr = f"box{self.n}@example.com"
        self.messages.setdefault(addr, [])
        return MailboxAccount(email=addr, account_id=addr)

    def get_current_ids(self, account) -> set:
        return set(self.messages.get(account.email, []))

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                      code_pattern=None) -> str:
        raise TimeoutError("not used here")

    def close(self) -> None:
        return None


class FakeAccount:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.platform = "chatgpt"
        self.extra: dict = {}
        self.user_id = ""
        self.token = ""


class FakePlatform:
    """复刻 `BasePlatform._prepare_registration_password` 的语义：
    调用方不给密码就现生成一个。生产里 ChatGPT 还会覆盖成更强的生成器，
    但「谁来生成、生成几次」这件事两边是一样的。
    """

    def __init__(self, world: "FakeWorld", proxy, mailbox, payload) -> None:
        self.world = world
        self.proxy = proxy
        self.mailbox = mailbox
        self.payload = payload

    # 生产里这是 BasePlatform 上的公开钩子（本次要加）：任务层拿它取一次
    # 周期密码，之后每次尝试原样传回去。
    def new_registration_password(self) -> str:
        self.world.generated += 1
        return f"pw-{self.world.generated}"

    def register(self, email=None, password=None):
        w = self.world
        attempt_no = len(w.attempts)
        pw = password or self.new_registration_password()
        identity = self.mailbox.get_email() if self.mailbox else SimpleNamespace(email=email or "")
        w.attempts.append({
            "email": identity.email,
            "password": pw,
            "extra": dict(self.payload.get("extra") or {}),
        })

        script = w.on_attempt[attempt_no] if attempt_no < len(w.on_attempt) else None
        if callable(script):
            script(self)

        outcome = w.outcomes[attempt_no] if attempt_no < len(w.outcomes) else w.default_outcome
        if outcome != "ok":
            raise RuntimeError(outcome)
        return FakeAccount(identity.email, pw)

    def set_logger(self, _fn):
        return None


class FakeProxyPool:
    def get_next(self, region: str = ""):
        return None

    def report_success(self, url):
        return None

    def report_fail(self, url):
        return None


class FakeWorld:
    def __init__(self, monkeypatch, *, outcomes, default_outcome="boom") -> None:
        self.outcomes = list(outcomes)
        self.default_outcome = default_outcome
        self.on_attempt: list = []
        self.attempts: list[dict] = []
        self.generated = 0
        self.saved: list = []
        self.mailbox = FakeMailbox()
        self.task = create_task(task_type="register", platform="chatgpt", payload={})
        self.logger = TaskLogger(self.task["id"])

        def _build(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
            return FakePlatform(self, resolved_proxy, shared_mailbox, payload)

        monkeypatch.setattr("application.tasks._build_platform_instance", _build)
        monkeypatch.setattr("application.tasks.save_account", self.saved.append)
        monkeypatch.setattr("application.tasks._save_task_log", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks._auto_followup_windsurf_payment", lambda **k: None)
        monkeypatch.setattr("application.tasks._auto_upload_cpa", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks._auto_push_any2api", lambda *a, **k: None)
        monkeypatch.setattr("application.tasks.get", lambda _name: FakePlatform)
        monkeypatch.setattr("core.proxy_pool.proxy_pool", FakeProxyPool())
        monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **kw: self.mailbox)
        monkeypatch.setattr(
            "infrastructure.provider_settings_repository.ProviderSettingsRepository"
            ".get_default_provider_key",
            lambda self, kind: "moemail",
        )

    def run(self, **payload):
        body = {"platform": "chatgpt"}
        body.update(payload)
        _execute_register_task(body, self.logger)
        return get_task(self.task["id"])

    @property
    def passwords(self) -> list[str]:
        return [a["password"] for a in self.attempts]

    @property
    def emails(self) -> list[str]:
        return [a["email"] for a in self.attempts]


def world(monkeypatch, **kw):
    return FakeWorld(monkeypatch, **kw)


def test_all_attempts_of_one_cycle_share_one_password(monkeypatch):
    """一轮三次尝试必须是**同一对**凭据。

    今天红：第 1 次用 pw-1 建号，第 2 次拿 pw-2 去登录同一个邮箱。
    """
    w = world(monkeypatch, outcomes=[])
    w.run(count=1, retry_count=2, max_attempts=3)

    assert len(w.passwords) == 3, f"应当跑满 3 次尝试，实际 {len(w.passwords)}"
    assert len(set(w.passwords)) == 1, (
        f"同一轮的三次尝试用了 {len(set(w.passwords))} 个不同密码: {w.passwords}；"
        "邮箱被钉到了周期级而密码没有——凭据对只钉住了一半"
    )


def test_a_new_cycle_gets_a_new_password(monkeypatch):
    """两个方向一起钉：轮内必须同一个，轮间必须换。

    第三个 assert 拦的是修过头——「把密码钉到任务级」，那样两个账号同密码。
    """
    w = world(monkeypatch, outcomes=["boom", "ok", "ok"])
    w.run(count=2, retry_count=1)

    assert len(w.passwords) == 3, f"应当是 2+1 三次尝试，实际 {len(w.passwords)}"
    assert w.passwords[0] == w.passwords[1], f"第一轮的重试换了密码: {w.passwords[:2]}"
    assert w.passwords[2] != w.passwords[0], (
        f"第二轮复用了第一轮的密码 {w.passwords[2]} —— 密码被钉到了任务级而不是周期级"
    )


def test_payload_password_still_wins(monkeypatch):
    """逃生口：调用方显式给了密码就不许生成。今天绿，拦的是「无条件生成」。"""
    w = world(monkeypatch, outcomes=[])
    w.run(count=1, retry_count=1, max_attempts=2, password="Given123!")

    assert w.passwords == ["Given123!", "Given123!"], f"实际 {w.passwords}"
    assert w.generated == 0, "payload 已经给了密码，不该再生成"


def test_a_created_account_routes_the_next_attempt_to_resume(monkeypatch):
    """第 1 次「账号建好了、OAuth 失败」之后，第 2 次不许从注册入口重放。

    今天红在 import：`RegistrationAttemptError` 还不存在。
    """
    from core.registration.errors import RegistrationAttemptError

    w = world(monkeypatch, outcomes=[])

    def _account_created_then_oauth_failed(platform):
        raise RegistrationAttemptError(
            "ChatGPT 注册未完成完整 OAuth callback",
            stage="account_created",
            email=w.attempts[-1]["email"],
            password=w.attempts[-1]["password"],
        )

    w.on_attempt = [_account_created_then_oauth_failed]
    w.run(count=1, retry_count=1, max_attempts=2)

    assert len(w.attempts) == 2, f"应当是 2 次尝试，实际 {len(w.attempts)}"
    resume = (w.attempts[1]["extra"] or {}).get("registration_resume") or {}
    assert resume.get("stage") == "account_created", (
        f"第 2 次尝试拿到的 extra 里没有周期检查点（实际 {resume!r}）——"
        "它会从注册入口重放，而那个邮箱在 OpenAI 那边已经是老账号了"
    )
    assert w.attempts[1]["password"] == w.attempts[0]["password"], (
        "恢复时必须带着建号时那把密码"
    )


def test_a_fresh_cycle_does_not_inherit_the_previous_resume(monkeypatch):
    """检查点是周期级的：下一轮换了邮箱，不许还带着上一轮的恢复标记。"""
    from core.registration.errors import RegistrationAttemptError

    w = world(monkeypatch, outcomes=[], default_outcome="boom")

    def _account_created(platform):
        raise RegistrationAttemptError(
            "oauth failed",
            stage="account_created",
            email=w.attempts[-1]["email"],
            password=w.attempts[-1]["password"],
        )

    w.on_attempt = [_account_created]
    w.run(count=2, retry_count=1, max_attempts=4)

    assert len(w.attempts) == 4, f"应当是两轮各 2 次，实际 {len(w.attempts)}"
    third = (w.attempts[2]["extra"] or {}).get("registration_resume") or {}
    assert not third, f"第二轮第一次尝试带着上一轮的检查点 {third!r}"
    assert w.emails[2] != w.emails[0], "第二轮换邮箱这条不许被改坏"


# ══════════════════════════════════════════════════════════════════════
# 插件层：检查点要传到浏览器 worker
# ══════════════════════════════════════════════════════════════════════

def test_browser_adapter_forwards_the_cycle_resume_stage():
    from core.base_platform import RegisterConfig
    from core.registration.models import RegistrationArtifacts, RegistrationContext
    from platforms.chatgpt.plugin import ChatGPTPlatform

    calls: dict = {}

    class FakeWorker:
        def run(self, **kwargs):
            calls.update(kwargs)
            return {"account_id": "a", "access_token": "t"}

    platform = object.__new__(ChatGPTPlatform)
    platform.config = RegisterConfig(
        executor_type="headless",
        extra={"registration_resume": {"stage": "account_created"}},
    )
    adapter = platform.build_browser_registration_adapter()
    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=platform,
        identity=SimpleNamespace(email="u@example.com", identity_provider="mailbox"),
        config=platform.config,
        email="u@example.com",
        password="Pinned123!",
        log_fn=lambda _m: None,
    )

    adapter.browser_register_runner(FakeWorker(), ctx, RegistrationArtifacts())

    assert calls.get("resume_stage") == "account_created", (
        f"worker.run 收到的参数是 {sorted(calls)} —— 本轮已建号这件事没传下去"
    )


def test_run_skips_the_signup_state_machine_when_resuming(monkeypatch):
    import platforms.chatgpt.browser_register as br

    flow_calls: list = []
    launches: list = []

    def _fake_flow(*args, **kwargs):
        flow_calls.append(1)
        return {"page_type": "chatgpt_home"}

    monkeypatch.setattr(br, "_browser_registration_flow", _fake_flow)
    monkeypatch.setattr(br, "Camoufox", lambda **kw: launches.append(kw) or _Unusable())
    monkeypatch.setattr(
        br.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser",
        lambda self, email, password: {
            "account_id": "acc", "access_token": "at",
            "refresh_token": "rt", "id_token": "it",
        },
    )

    worker = br.ChatGPTBrowserRegister(headless=True, log_fn=lambda *_a, **_k: None)
    result = worker.run(email="u@example.com", password="Pinned123!", resume_stage="account_created")

    assert flow_calls == [], "恢复态下仍然跑了一遍注册状态机"
    assert launches == [], "恢复态下仍然开了注册用的那个浏览器"
    assert result["access_token"] == "at"


class _Unusable:
    def __enter__(self):
        raise AssertionError("恢复态不该进入注册浏览器上下文")

    def __exit__(self, *a):
        return False


def test_run_reports_a_created_account_when_oauth_fails(monkeypatch):
    """OAuth 失败时抛的异常要让调用方分得出「账号已经建出来了」。"""
    from core.registration.errors import RegistrationAttemptError
    import platforms.chatgpt.browser_register as br

    monkeypatch.setattr(br, "_browser_registration_flow", lambda *a, **k: {"page_type": "chatgpt_home"})
    monkeypatch.setattr(br, "_get_cookies", lambda page: {})
    monkeypatch.setattr(br, "Camoufox", lambda **kw: _FakeBrowser())
    monkeypatch.setattr(
        br.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser",
        lambda self, email, password: None,
    )

    worker = br.ChatGPTBrowserRegister(headless=True, log_fn=lambda *_a, **_k: None)
    with pytest.raises(RegistrationAttemptError) as err:
        worker.run(email="u@example.com", password="Pinned123!")

    assert err.value.stage == "account_created"
    assert err.value.password == "Pinned123!"
    assert "已拒绝回退" in str(err.value)


class _FakeBrowser:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def new_page(self):
        return SimpleNamespace(url="https://chatgpt.com/", evaluate=lambda *a, **k: "")


# ══════════════════════════════════════════════════════════════════════
# 状态机层：验证码分支
# ══════════════════════════════════════════════════════════════════════

class _OtpPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.gotos: list[str] = []

    def evaluate(self, *a, **k):
        return ""

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    def wait_for_load_state(self, *a, **k):
        return None


VERIFY_URL = "https://auth.openai.com/create-account/email-verification"


def _otp_flow_env(monkeypatch, br):
    monkeypatch.setattr(br, "_seed_browser_device_id", lambda *a, **k: None)
    monkeypatch.setattr(br, "_get_cookies", lambda page: {})
    monkeypatch.setattr(
        br, "_start_browser_signup_via_page",
        lambda page, email, log: {
            "page_type": "email_otp_verification",
            "continue_url": VERIFY_URL,
            "method": "GET",
            "current_url": VERIFY_URL,
            "payload": {},
            "raw": {},
        },
    )


def test_email_otp_branch_lands_on_the_otp_page_before_typing(monkeypatch):
    """状态说「下一步是验证码页」，浏览器还停在密码页 —— 必须先跳过去。

    兄弟分支 about_you 就是这么写的（`if "about-you" not in page.url: goto`），
    验证码这一条漏了，于是往密码页的 DOM 里打验证码。
    """
    import platforms.chatgpt.browser_register as br

    _otp_flow_env(monkeypatch, br)
    page = _OtpPage("https://auth.openai.com/create-account/password")
    seen: dict = {}

    # 假页面没有 Playwright 的 locator：这里只测「有没有跳过去」，
    # 输入框存不存在由下一条测试负责。
    monkeypatch.setattr(
        br, "_wait_for_any_selector",
        lambda p, selectors, timeout=12: "input[inputmode='numeric']",
    )

    class _Stop(Exception):
        pass

    def _fake_submit(p, code, log):
        seen["url"] = p.url
        raise _Stop()

    monkeypatch.setattr(br, "_submit_otp_via_page", _fake_submit)

    with pytest.raises(_Stop):
        br._browser_registration_flow(
            page, "u@example.com", "Pinned123!",
            lambda: "123456", None, lambda *_a, **_k: None,
        )

    assert "email-verification" in seen["url"], (
        f"开始填验证码时浏览器停在 {seen['url']} —— 那个页面上没有验证码输入框"
    )
    assert VERIFY_URL in page.gotos, f"没有跳转到验证码页，goto 记录={page.gotos}"


def test_a_missing_otp_input_does_not_burn_a_code(monkeypatch):
    """页面上没有输入框时，不许先把验证码取出来再失败。

    今天红：验证码先被 `otp_callback()` 消费掉，然后才报
    「验证码页未找到可填写输入框」——那一封码就废了，重试也拿不回来。
    """
    import platforms.chatgpt.browser_register as br

    _otp_flow_env(monkeypatch, br)
    page = _OtpPage(VERIFY_URL)
    otp_calls: list = []

    monkeypatch.setattr(br, "_wait_for_any_selector", lambda p, selectors, timeout=12: None)
    monkeypatch.setattr(
        br, "_submit_otp_via_page",
        lambda p, code, log: {
            "ok": False, "status": 0, "url": p.url, "data": None,
            "text": "验证码页未找到可填写输入框",
        },
    )

    def _otp():
        otp_calls.append(1)
        return "123456"

    with pytest.raises(RuntimeError):
        br._browser_registration_flow(
            page, "u@example.com", "Pinned123!", _otp, None, lambda *_a, **_k: None,
        )

    assert otp_calls == [], (
        f"验证码被取走了 {len(otp_calls)} 次才发现页面上没有输入框 —— 这封码作废了"
    )
