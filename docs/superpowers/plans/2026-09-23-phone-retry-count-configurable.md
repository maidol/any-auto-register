# 接码换号重试次数 (phone_retry_count) 可配置化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持在发起注册任务时单次配置「换号重试次数 (`phone_retry_count`)」，默认 2 次（总尝试最多 3 个号码）；允许设置为 0（彻底禁用超时换号，失败立即终止）；允许范围 0~10。

**Architecture:** 
1. 在 `RegisterTaskRequest` 声明显式一级字段 `phone_retry_count: int = Field(default=2, ge=0, le=10)`，并同步到 `customer_portal_api` 保持模型对齐。
2. 在 `application/tasks.py` 将字段存入 `RegisterConfig.extra` 透传给底层。
3. `platforms/chatgpt/plugin.py` 计算 `max_phone_attempts = max(1, 1 + phone_retry_count)` 并传给 `ChatGPTBrowserRegister`。
4. `ChatGPTBrowserRegister` 在执行浏览器注册及 OAuth 遇到 `add_phone` 时透传 `max_phone_attempts` 给 `_handle_add_phone_challenge`。
5. 前端 `Accounts.tsx` 与 `Register.tsx` 提供输入框，默认 2，min=0，max=10。

**Tech Stack:** Python 3.11, FastAPI/Pydantic, React/TypeScript, Playwright/Camoufox, pytest

---

## 任务拆解

### Task 1: API 请求体与 HTTP 边界测试 (TDD)

**Files:**
- Modify: `api/task_commands.py:17-41`
- Modify: `customer_portal_api/app/routers/admin.py:20-40`
- Modify: `customer_portal_api/app/routers/app_api.py:16-36`
- Test: `tests/test_register_request_boundary.py`

- [ ] **Step 1: 编写红测试**

在 `tests/test_register_request_boundary.py` 中更新 `FRONTEND_BODY` 与 `STRATEGY_KEYS`，加入 `phone_retry_count`，并增加验证测试：
```python
def test_phone_retry_count_survives_model_dump():
    dumped = RegisterTaskRequest(**FRONTEND_BODY).model_dump()
    assert dumped.get("phone_retry_count") == 2

def test_phone_retry_count_rejects_out_of_range():
    with pytest.raises(Exception):
        RegisterTaskRequest(**dict(FRONTEND_BODY, phone_retry_count=-1))
    with pytest.raises(Exception):
        RegisterTaskRequest(**dict(FRONTEND_BODY, phone_retry_count=11))
```

- [ ] **Step 2: 运行测试验证红测**

Run: `.venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_register_request_boundary.py`
Expected: FAIL（报错缺少字段或字段丢弃）

- [ ] **Step 3: 实现 API 模型字段与 Portal 模型同步**

在 `api/task_commands.py` 的 `RegisterTaskRequest` 中添加：
```python
phone_retry_count: int = Field(default=2, ge=0, le=10)
```
在 `customer_portal_api/app/routers/admin.py` 和 `customer_portal_api/app/routers/app_api.py` 的 `RegisterTaskRequest` 中同样添加：
```python
phone_retry_count: int = 2
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_register_request_boundary.py`
Expected: 全部 PASS

- [ ] **Step 5: 检查并暂存**

Run: `git status --short` 确认改动范围。

---

### Task 2: 任务执行层透传与平台适配器 (TDD)

**Files:**
- Modify: `application/tasks.py:500-515`
- Modify: `platforms/chatgpt/plugin.py:160-175`
- Test: `tests/test_chatgpt_phone_channel.py`

- [ ] **Step 1: 编写红测试**

在 `tests/test_chatgpt_phone_channel.py` 中增加适配器和参数传递测试：
```python
def test_chatgpt_adapter_respects_phone_retry_count_from_extra():
    from core.base_platform import RegisterConfig, RegistrationContext
    from platforms.chatgpt.plugin import ChatGPTPlatform
    
    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        identity=None,
        password="pw",
        proxy=None,
        executor_type="browser",
        log=lambda *a: None,
        extra={"phone_retry_count": 0},
    )
    platform = ChatGPTPlatform(config=RegisterConfig(extra={"phone_retry_count": 0}))
    adapter = platform.build_browser_registration_adapter()
    artifacts = type("Artifacts", (), {"otp_callback": None, "phone_callback": None})()
    worker = adapter.browser_worker_builder(ctx, artifacts)
    assert worker.max_phone_attempts == 1
```

- [ ] **Step 2: 运行测试验证红测**

Run: `.venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_chatgpt_phone_channel.py::test_chatgpt_adapter_respects_phone_retry_count_from_extra`
Expected: FAIL (`AttributeError: 'ChatGPTBrowserRegister' object has no attribute 'max_phone_attempts'`)

- [ ] **Step 3: 实现 tasks.py 与 plugin.py 透传**

在 `application/tasks.py` 的 `_build_platform_instance`：
```python
extra = dict(payload.get("extra") or {})
if "phone_retry_count" in payload:
    extra["phone_retry_count"] = payload["phone_retry_count"]
elif "phone_retry_count" not in extra:
    extra["phone_retry_count"] = 2
```
在 `platforms/chatgpt/plugin.py` 的 `build_browser_registration_adapter`：
```python
phone_retry_count = int(ctx.extra.get("phone_retry_count", 2) if ctx.extra.get("phone_retry_count") is not None else 2)
max_phone_attempts = max(1, 1 + phone_retry_count)
```
并在实例化 `ChatGPTBrowserRegister` 时传入 `max_phone_attempts=max_phone_attempts`。

- [ ] **Step 4: 运行测试验证**

Run: `.venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_chatgpt_phone_channel.py::test_chatgpt_adapter_respects_phone_retry_count_from_extra`
Expected: PASS

---

### Task 3: ChatGPT 浏览器注册执行层接入与重试行为验证 (TDD)

**Files:**
- Modify: `platforms/chatgpt/browser_register.py:2120-2130, 4190-4215, 4230-4328`
- Test: `tests/test_chatgpt_phone_channel.py`

- [ ] **Step 1: 编写重试控制的单元测试**

在 `tests/test_chatgpt_phone_channel.py` 增加：
```python
def test_code_timeout_with_zero_retry_terminates_immediately_without_rearm(monkeypatch):
    callback = _CountryTracePhoneCallback()
    attempts = []

    def _fake_attempt(_page, _phone_callback, **_kwargs):
        attempts.append(1)
        raise br.HeroSmsCodeTimeoutError("act_timeout_1")

    monkeypatch.setattr(br, "_do_add_phone_attempt", _fake_attempt)
    monkeypatch.setattr(br.time, "sleep", lambda *_args: None)
    with pytest.raises(br.HeroSmsCodeTimeoutError, match="act_timeout_1"):
        br._handle_add_phone_challenge(
            _NavPage([]), callback,
            device_id="d", user_agent="ua", log=_log, max_phone_attempts=1,
        )

    # 彻底禁用重试：仅尝试 1 次号码，不触发任何 rearm
    assert len(attempts) == 1
    assert callback.events == []


def test_code_timeout_with_custom_retry_count(monkeypatch):
    callback = _CountryTracePhoneCallback()
    attempts = []

    def _fake_attempt(_page, _phone_callback, **_kwargs):
        attempts.append(1)
        raise br.HeroSmsCodeTimeoutError(f"act_timeout_{len(attempts)}")

    monkeypatch.setattr(br, "_do_add_phone_attempt", _fake_attempt)
    monkeypatch.setattr(br.time, "sleep", lambda *_args: None)
    with pytest.raises(br.HeroSmsCodeTimeoutError, match="act_timeout_2"):
        br._handle_add_phone_challenge(
            _NavPage([]), callback,
            device_id="d", user_agent="ua", log=_log, max_phone_attempts=2,
        )

    # 重试 1 次（总共 2 次）：尝试 2 次，rearm 1 次
    assert len(attempts) == 2
    assert callback.events == [("rearm", "86")]
```

- [ ] **Step 2: 运行测试验证**

Run: `.venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_chatgpt_phone_channel.py::test_code_timeout_with_zero_retry_terminates_immediately_without_rearm tests/test_chatgpt_phone_channel.py::test_code_timeout_with_custom_retry_count`
Expected: PASS（底层函数原本就接受 `max_phone_attempts` 参数）

- [ ] **Step 3: 接通 `ChatGPTBrowserRegister` 到调用点**

修改 `platforms/chatgpt/browser_register.py`：
1. `ChatGPTBrowserRegister.__init__` 增加 `max_phone_attempts: int = 3`，赋值 `self.max_phone_attempts = max_phone_attempts`。
2. `_browser_registration_flow` 签名增加 `max_phone_attempts: int = 3`，在调用 `_handle_add_phone_challenge` 时传出 `max_phone_attempts=max_phone_attempts`。
3. `ChatGPTBrowserRegister.run` 调用 `_browser_registration_flow` 时传入 `self.max_phone_attempts`。
4. `_retry_oauth_fresh_browser` 调用 `_do_codex_oauth` 时传入 `max_phone_attempts`。
5. `_do_codex_oauth` 签名增加 `max_phone_attempts: int = 3`，在遇到 `add_phone` 调用 `_handle_add_phone_challenge` 时传出 `max_phone_attempts=max_phone_attempts`。

- [ ] **Step 4: 运行全套相关测试**

Run: `.venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_chatgpt_phone_channel.py tests/test_sms_provider.py`
Expected: 全部 PASS

---

### Task 4: 前端界面表单与构建

**Files:**
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `frontend/src/pages/Register.tsx`

- [ ] **Step 1: 修改 Accounts.tsx**
在 `RegisterModal` 中：
1. 添加状态：`const [phoneRetryCount, setPhoneRetryCount] = useState<number>(2)`
2. 在表单提交 `apiFetch('/tasks/register', ...)` 的 body 中添加 `phone_retry_count: phoneRetryCount`。
3. 在策略网格中添加输入项：
```tsx
<div>
  <label className="text-xs text-[var(--text-muted)] block mb-1">换号重试次数</label>
  <input type="number" min={0} max={10} value={phoneRetryCount}
    onChange={e => setPhoneRetryCount(Number(e.target.value))}
    className="control-surface control-surface-compact text-center" />
</div>
```

- [ ] **Step 2: 修改 Register.tsx**
1. 在 `DEFAULT_FORM` 中添加 `phone_retry_count: 2`。
2. 在表单策略卡片中添加：
```tsx
<Input label="换号重试次数" k="phone_retry_count" type="number" min={0} max={10} />
```

- [ ] **Step 3: 执行前端编译构建**

Run: `cd frontend && npm run build && cd ..`
Expected: `✓ built in ...ms`，无类型报错。

---

### Task 5: 全量回归与变异测试

**Files:**
- Test: `tests/` 全量套件

- [ ] **Step 1: 运行全量 pytest 测试**

Run: `.venv311/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -15`
Expected: 8 failed, 285+ passed（既有 8 条红保持完全一致，新增测试全部通过）。

- [ ] **Step 2: 审查 git status**

Run: `git status --short`
核对修改文件列表，确认无意外改动。
