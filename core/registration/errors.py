from __future__ import annotations


class RegistrationError(RuntimeError):
    """注册流程基础异常。"""


class IdentityResolutionError(RegistrationError):
    """身份解析失败。"""


class CaptchaConfigurationError(RegistrationError):
    """验证码配置不可用。"""


class OtpTimeoutError(RegistrationError):
    """验证码等待超时。"""


class BrowserReuseRequiredError(RegistrationError):
    """无头 OAuth 缺少可复用浏览器会话。"""


class RegistrationUnsupportedError(RegistrationError):
    """当前平台或执行器不支持该注册路径。"""


#: 注册失败账号在列表里的细分阶段（overview.failure_stage）。只管展示，
#: 和下面的 RegistrationAttemptError.stage 是两件事：stage 决定下一次尝试
#: 要不要跳过注册入口，这里只回答「ChatGPT 那边有没有这个账号」。
FAILURE_NOT_CREATED = "not_created"
FAILURE_CREATED = "created_other_failed"
FAILURE_OAUTH = "created_oauth_failed"
FAILURE_UNKNOWN = "unknown"


class RegistrationAttemptError(RegistrationError):
    """一次尝试失败了，并且带着「这次走到哪儿」这件事一起失败。

    通用异常只能告诉调用方「这次没成」，而周期内重试要做的决定不同：
    账号如果已经在平台那边建出来了，下一次就不该再从注册入口走一遍 ——
    那个邮箱已经是老账号，注册入口只会把流程带进登录/验证码分支，
    而现场看到的就是「原来的逻辑不适用」。

    stage 取值（按流程推进顺序）：
      ""                 未知，按全新注册处理
      "signup_started"   邮箱已提交，账号还没建出来
      "account_created"  账号已在平台侧建好，后续只差 OAuth / 后处理
    """

    #: 认为「平台那边已经有这个账号了」的阶段
    RESUMABLE_STAGES = ("account_created",)

    def __init__(
        self,
        message: str,
        *,
        stage: str = "",
        failure_stage: str = "",
        email: str = "",
        password: str = "",
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.failure_stage = failure_stage
        self.email = email
        self.password = password

    @property
    def account_created(self) -> bool:
        return self.stage in self.RESUMABLE_STAGES

