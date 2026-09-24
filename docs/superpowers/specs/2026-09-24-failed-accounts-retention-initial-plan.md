# 需求分析与初步实现方案：保留注册失败账号并标明细分失败状态

## 1. 需求来源与核心目标

需求描述（`temp/20260924-需求.txt`）：
> 1. 注册失败的 email 账号，要保留在账号列表，标明注册失败，失败状态主要有：
>    - 账号在 chatgpt 端未新建；
>    - 已新建；
>    - 已新建但 codex oauth 获取 rt 失败。

### 核心痛点与业务价值
1. **防止“孤儿账号”失联**：此前 ChatGPT 注册中，若账号已在平台侧建好（提交了姓名、年龄、甚至完成了手机验证），但在最后获取 Codex CLI OAuth Token（access_token/refresh_token）时失败，系统因未返回完整 `Account` 而直接抛出异常，**不会往 `accounts` 表落库**。这导致密码和邮箱只存在于瞬时日志中，变成了无法在账号列表中管理的“孤儿账号”，无法后续重试登录或人工找回。
2. **邮箱占用状态追踪**：未成功的邮箱无法在账号列表中看见，运维无法直观感知哪些邮箱已被使用过、卡在什么阶段。

---

## 2. 状态定义与模型设计

### 2.1 状态分类与枚举定义
建议统一纳入现有账号图谱（`account_graph`）与生命周期状态（`lifecycle_status`）：

1. **顶级生命周期状态**：
   - `lifecycle_status = "failed"`（或 `"register_failed"`，与现有的 `"registered"`, `"trial"`, `"subscribed"`, `"expired"`, `"invalid"` 并列）。
2. **细分子状态（存储在 `overview.failure_stage` 中）**：
   - `not_created`（未新建）：在 ChatGPT 注册入口、验证码、或初始表单阶段即失败，OpenAI 侧尚未创建该账号。
   - `created_other_failed`（已新建）：平台侧账号已创建成功，但在随后的流程（如 onboarding、页面状态异常等非 OAuth 环节）失败。
   - `created_oauth_failed`（已新建但 Codex OAuth 获取 RT 失败）：账号已建好，但执行全新的 Codex CLI OAuth 流程时失败或未完成完整 callback。
3. **展示状态与徽章（`display_status` / `badges`）**：
   - 列表主状态显示「注册失败」，并带危险/警告色徽章（Danger/Warning Badge）。
   - 副状态/提示明细展示对应子阶段：`[未新建]`、`[已建号]`、`[已建号·OAuth失败]`。
   - `overview.failure_reason`：记录失败时的具体错误摘要，方便在账号详情弹窗或悬浮提示中查看。

---

## 3. 系统各层改动分析

### 3.1 异常体系与执行器阶段识别 (`core/registration/errors.py` & `platforms/chatgpt/`)
- 当前 `RegistrationAttemptError.stage` 现有取值：`"signup_started"`, `"account_created"`。
- **调整建议**：
  - 将阶段明确细化为三类：
    1. `not_created`: 注册状态机失败，且尚未完成 signup（原 `signup_started` 统一归入 `not_created`）。
    2. `created_other_failed`: 注册状态机已完成（`account_created = True`），但在其他非 OAuth 错误分支抛错。
    3. `created_oauth_failed`: 账号已创建成功，但 `_retry_oauth_fresh_browser` 获取 OAuth 失败抛出的异常。
  - `RegistrationAttemptError` 确保始终携带捕获到的 `email` 和 `password`。

### 3.2 任务编排与落库时机 (`application/tasks.py`)
- 当前逻辑：只有成功时才调用 `save_account(account)`。
- **改动设计**：
  - 在 `_register_once` 捕获到异常时，或者在单个账号周期（Cycle）的所有尝试彻底结束且 `record.success is False` 时：
    - 获取本周期尝试的 `email` 和 `cycle["password"]`。
    - 若 `email` 存在，调用专门的落库函数（例如 `save_failed_account(...)` 或构建一个带 `lifecycle_status="failed"` 的 Account 对象存入数据库）。
    - 记录 `overview`：包含 `failure_stage`、`failure_reason`、`failed_at` 等信息。
  - **重要约束（重试与幂等）**：
    - 如果第 1 次尝试失败了（例如 OAuth 失败），但该周期配置了重试（`retry_count > 0`），第 2 次重试成功完成了注册：
      - 最终成功的 `save_account` 会更新同平台同邮箱记录，将 `lifecycle_status` 正常置为 `"registered"`，清除失败标记。
    - 如果重试依然失败，则保留最终的失败状态与详细原因。

### 3.3 数据库模型与图谱兼容 (`core/db.py` & `core/account_graph.py`)
- `AccountModel` 本身不需要加列（它只存 `platform`, `email`, `password`, `user_id` 等核心凭据）。
- `AccountOverviewModel` 的 `lifecycle_status` 支持索引，允许快速按 `lifecycle_status == "failed"` 过滤查询。
- `core/account_graph.py`：
  - `_derive_display_status` 增加对 `"failed"` 的映射。
  - `matches_status_filter` 支持筛选 `"failed"`。
- 确保导出与同步守卫不受影响：
  - `Sub2ApiSyncService` 仅同步 `AUTO_SYNC_STATUSES = {"registered", "trial", "subscribed"}`，天然排除 `"failed"`，不会将未成功的账号导入外部系统。

### 3.4 前端呈现 (`frontend/`)
- `Accounts.tsx`：
  - 状态筛选下拉框增加「注册失败」选项。
  - 账号行状态徽章渲染：根据 `lifecycle_status === 'failed'` 渲染红色 Badge，并在后面附带细分阶段小标签（如 `未新建` / `已建号·无Token`）。
  - 账号详情弹窗（`DetailModal`）展示具体的失败原因与发生时间。

---

## 4. 提交审查与提问清单（待 workspace 反馈）

1. **顶级状态命名确认**：
   - 方案 A：使用 `lifecycle_status = "failed"`，细分状态放在 `overview.failure_stage`（推荐，对其他导出和统计系统的改动最小）。
   - 方案 B：直接将三个状态作为独立的顶级 `lifecycle_status`（如 `failed_not_created`, `failed_created`, `failed_oauth`）。
2. **多轮重试下的落库策略**：
   - 是否只在周期彻底失败（所有 retry 尝试耗尽后）才落库为失败账号，还是每次 attempt 失败都立即写库？（推荐在每次失败时即时 upsert，保证即使中途强行中断任务也能在库中找到已生成的邮箱与密码）。
3. **其他平台适配**：
   - 该需求主要针对 ChatGPT，其他平台（如 Windsurf、Cursor 等）失败时是否按统一的 `not_created` 兜底落库？
