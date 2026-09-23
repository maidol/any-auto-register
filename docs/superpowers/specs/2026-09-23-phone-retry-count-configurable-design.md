# 设计文档：支持任务级配置接码换号重试次数 (phone_retry_count)

## 1. 概述与背景

在当前 ChatGPT 注册流程中，当遭遇手机号验证（`add_phone`）且短信验证码超时或遇到渠道不可用时，系统会在 `_handle_add_phone_challenge` 中进行换号重试。
此前实现中，重试次数上限硬编码为 `max_phone_attempts: int = 3`（1 次初始获取 + 2 次换号重试），无法由用户根据接码平台预算、号码库质量或业务需求进行调整。

本设计旨在支持在发起注册任务时单次配置「换号重试次数 (`phone_retry_count`)」：
- 默认为 `2`（对应总计最多 3 次号码尝试）。
- 设为 `0` 时，彻底禁用换号重试：单个号码一旦 180s 等码超时或失败立即终止流程，不再租用新号码。
- 允许配置范围为 `0 ~ 10`。

---

## 2. 总体架构与数据流

```
[前端: Accounts.tsx / Register.tsx]
      │
      │ POST /api/tasks/register { ..., phone_retry_count: N }
      ▼
[API 层: api/task_commands.py (RegisterTaskRequest)]
      │
      │ 校验 0 <= phone_retry_count <= 10，并传给 command_service
      ▼
[任务执行层: application/tasks.py (_build_platform_instance)]
      │
      │ 提取 payload["phone_retry_count"]，写入 extra["phone_retry_count"]
      ▼
[平台适配器: platforms/chatgpt/plugin.py]
      │
      │ 计算 max_phone_attempts = 1 + phone_retry_count
      │ 注入 ChatGPTBrowserRegister(..., max_phone_attempts=...)
      ▼
[浏览器注册: platforms/chatgpt/browser_register.py]
      │
      │ _browser_registration_flow / _do_codex_oauth
      │ 调用 _handle_add_phone_challenge(..., max_phone_attempts=self.max_phone_attempts)
      ▼
[执行结果]
      N = 0: 仅 1 次尝试，超时立即终止退出；
      N > 0: 最多 1 + N 次尝试，超时 rearm 并换号。
```

---

## 3. 详细设计

### 3.1 API 契约与校验 (`api/task_commands.py`)
在 `RegisterTaskRequest` 中新增显式一级字段：
```python
class RegisterTaskRequest(BaseModel):
    ...
    retry_count: int = 0
    retry_interval_seconds: float = 0.0
    account_interval_seconds: float = 0.0
    phone_retry_count: int = Field(default=2, ge=0, le=10, description="接码换号重试次数 (0表示不重试)")
    proxy_strategy: str = "round_robin"
    clean_browser_context: bool = True
    require_proxy: bool = False
```
同步在 `customer_portal_api/app/routers/admin.py` 及 `app_api.py` 的 `RegisterTaskRequest` 中补充此字段，避免模型反向序列化丢字段。

### 3.2 任务上下文透传 (`application/tasks.py`)
在 `_build_platform_instance` 中：
```python
extra = dict(payload.get("extra") or {})
if "phone_retry_count" in payload:
    extra["phone_retry_count"] = payload["phone_retry_count"]
elif "phone_retry_count" not in extra:
    extra["phone_retry_count"] = 2
```
将字段安全注入 `RegisterConfig.extra`，供各平台适配器使用。

### 3.3 平台接入 (`platforms/chatgpt/plugin.py`)
在 `build_browser_registration_adapter` 中计算实际尝试上限：
```python
phone_retry_count = int(ctx.extra.get("phone_retry_count", 2) if ctx.extra.get("phone_retry_count") is not None else 2)
max_phone_attempts = max(1, 1 + phone_retry_count)

browser_worker_builder=lambda ctx, artifacts: __import__("platforms.chatgpt.browser_register", fromlist=["ChatGPTBrowserRegister"]).ChatGPTBrowserRegister(
    headless=(ctx.executor_type == "headless"),
    proxy=ctx.proxy,
    otp_callback=artifacts.otp_callback,
    phone_callback=artifacts.phone_callback,
    log_fn=ctx.log,
    max_phone_attempts=max_phone_attempts,
)
```

### 3.4 浏览器流程执行 (`platforms/chatgpt/browser_register.py`)
1. `ChatGPTBrowserRegister.__init__` 增加 `max_phone_attempts: int = 3`：
   ```python
   def __init__(
       self,
       *,
       headless: bool,
       proxy: Optional[str] = None,
       otp_callback: Optional[Callable[[], str]] = None,
       phone_callback: Optional[Callable[[], str]] = None,
       log_fn: Callable[[str], None] = print,
       max_phone_attempts: int = 3,
   ):
       ...
       self.max_phone_attempts = max_phone_attempts
   ```
2. 在 `_browser_registration_flow` 接收并在调用 `_handle_add_phone_challenge` 时传出：
   ```python
   state = _handle_add_phone_challenge(
       page,
       phone_callback,
       device_id=device_id,
       user_agent=user_agent,
       log=log,
       resume_url=f"{CHATGPT_APP}/",
       max_phone_attempts=max_phone_attempts,
   )
   ```
3. 在 `_retry_oauth_fresh_browser` 及其 `_do_codex_oauth` 中透传 `max_phone_attempts`。

### 3.5 前端用户界面
1. **`frontend/src/pages/Accounts.tsx`（自动注册弹窗）**：
   - 增加状态：`const [phoneRetryCount, setPhoneRetryCount] = useState<number>(2)`
   - 在策略输入区域增加：
     ```tsx
     <div>
       <label className="text-xs text-[var(--text-muted)] block mb-1">换号重试次数</label>
       <input type="number" min={0} max={10} value={phoneRetryCount}
         onChange={e => setPhoneRetryCount(Number(e.target.value))}
         className="control-surface control-surface-compact text-center" />
     </div>
     ```
   - 请求体加入 `phone_retry_count: phoneRetryCount`。
2. **`frontend/src/pages/Register.tsx`（任务发起页）**：
   - 表单默认值 `DEFAULT_FORM` 补充 `phone_retry_count: 2`。
   - 策略配置网格补充对应输入框，支持配置。

---

## 4. 测试与验证策略

1. **HTTP 边界与校验测试** (`tests/test_register_request_boundary.py`)：
   - 验证 `phone_retry_count` 传入 0, 2, 10 均可正常通过验证。
   - 验证负数（如 `-1`）或超标数值（如 `11`）被 Pydantic 拒绝（422/ValidationError）。
2. **换号重试核心逻辑测试** (`tests/test_chatgpt_phone_channel.py`)：
   - **禁用换号（0 次重试）**：`max_phone_attempts = 1` 时发生 `HeroSmsCodeTimeoutError`，验证仅尝试 1 个号码且不调用 `rearm`，直接抛出异常终止。
   - **换号 1 次（1 次重试）**：`max_phone_attempts = 2` 时发生超时，验证尝试 2 次号码后终止。
   - **适配器传递验证**：验证从 `RegisterConfig.extra` 中读取并传递给 `ChatGPTBrowserRegister`。
3. **前端构建验证**：
   - 运行 `npm run build` 确保 TypeScript 类型与打包完全通过。
4. **全量回归**：
   - 运行 pytest 全量测试，确认既有测试无回归红线。
