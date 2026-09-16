"""ChatGPT 协议邮箱注册 worker。"""
from __future__ import annotations

from typing import Callable

from platforms.chatgpt.register import RegistrationEngine


class _MailboxEmailService:
    def __init__(self, *, mailbox, mailbox_account, provider: str):
        self.service_type = type("ST", (), {"value": provider})()
        self._mailbox = mailbox
        self._mailbox_account = mailbox_account
        self._acct = None
        self._floor_ids: set = set()

    def _snapshot_ids(self, acct) -> set:
        try:
            return set(self._mailbox.get_current_ids(acct) or set())
        except Exception:
            return set()

    def create_email(self, config=None):
        self._acct = self._mailbox_account
        # 注册开始前收件箱里已有的邮件一律不算数
        self._floor_ids = self._snapshot_ids(self._acct)
        return {
            "email": self._mailbox_account.email,
            "service_id": getattr(self._mailbox_account, "account_id", ""),
            "token": getattr(self._mailbox_account, "account_id", ""),
        }

    def get_verification_code(self, email=None, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
        acct = self._acct or self._mailbox_account
        # 等待前的快照只作为成功后的地板下限，不能作为 before_ids 传入。
        pre_ids = self._snapshot_ids(acct)
        # 原来这里既不传 before_ids 也不用 otp_sent_at（签名收下就丢掉），
        # 于是 RegistrationEngine 第二次要码时立刻命中第一封邮件里的旧验证码。
        code = self._mailbox.wait_for_code(
            acct,
            keyword="",
            timeout=timeout,
            before_ids=set(self._floor_ids),
            code_pattern=pattern,
        )
        if code:
            # 只升不降：快照失败时返回空集，不能清空已有地板。
            self._floor_ids |= pre_ids | self._snapshot_ids(acct)
        return code

    def update_status(self, success, error=None):
        return None

    @property
    def status(self):
        return None


class ChatGPTProtocolMailboxWorker:
    def __init__(
        self,
        *,
        mailbox,
        mailbox_account,
        provider: str,
        proxy_url: str | None = None,
        log_fn: Callable[[str], None] = print,
    ):
        if not mailbox or not mailbox_account:
            raise ValueError("ChatGPT 注册流程依赖 mailbox provider，当前未获取到邮箱账号")
        email_service = _MailboxEmailService(
            mailbox=mailbox,
            mailbox_account=mailbox_account,
            provider=provider,
        )
        self.engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=log_fn,
        )

    def run(self, *, email: str, password: str):
        self.engine.email = email
        self.engine.password = password
        result = self.engine.run()
        if not result or not result.success:
            raise RuntimeError(result.error_message if result else "注册失败")
        return result
