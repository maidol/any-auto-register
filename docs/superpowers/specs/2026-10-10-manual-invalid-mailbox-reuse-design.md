# 手动添加无效邮箱复用设计

**日期：** 2026-10-10  
**状态：** 已获用户确认，待 `my-agent-workspace` 评审  
**需求来源：** `temp/需求/20261010-需求.txt`

## 需求

1. 自动注册弹窗增加默认关闭的开关；开启时允许复用账号列表中未完成注册的邮箱，或手动添加且检测为无效的邮箱。
2. 账号列表应标出手动新增来源，来源标记必须可持久化；既有记录来源未知，不推断为手动。
3. 两类候选复用前均通过当前自动注册任务所选的临时邮箱服务确认该地址是否已存在/可收件；明确不存在时，按原邮箱地址创建邮箱资源。随后仍以账号记录保存的邮箱和密码执行注册。

## 代码现状

- `frontend/src/pages/Accounts.tsx` 的 `Accounts.AddModal` 当前 POST `/accounts`，没有写入来源；账号列表已有生命周期、有效性徽标。
- 账号 overview 存在 `AccountOverviewModel.summary_json`，`core.account_graph.patch_account_graph` 对 overview 做增量合并；可在 overview 中持久化轻量来源信息，不需要新增 SQL 列。
- `application.tasks._execute_register_task` 已有 ChatGPT 失败账号复用逻辑，`_claim_failed_account` 按邮箱 provider/读取能力筛选；当前失败记录按既有行为自动复用。
- `core.base_mailbox.create_mailbox` 可返回 `FallbackMailbox`；它把默认 provider 与其他启用 provider 组合。手动邮箱复用必须定向到任务所选的主 provider，不能因 fallback 而用别的服务创建地址。
- `core.base_mailbox.CFWorkerMailbox` 已有基于域名的 `can_read_address`、按地址读取邮件的 admin 接口，以及 `POST /admin/new_address` 创建入口（现实现用随机 local-part）。Workspace 评审依据 `cloudflare_temp_email` main @ `be7ba25a` 核实上游另有 `GET /admin/address` 搜索与指定 `name` 创建能力；部署版本需要只读探测确认。列表是子串搜索，必须客户端精确匹配。

## 设计决策

### 来源和可见性

手动新增账号时，前端在 `overview` 中写 `account_origin: "manual"`。该值由现有 overview JSON 持久化并随账号列表返回；账号行显示“手动添加”标识。该元数据只用于账号来源判断，不改变生命周期或有效性。旧记录缺少该字段时视为来源未知。导入账号及注册流程创建的账号不标记为手动。

### 自动注册选项

在 `Accounts.RegisterModal` 加默认关闭的布尔开关“复用未完成注册或手动添加的邮箱”，随 `/tasks/register` payload 写入 `extra.reuse_manual_invalid_emails`。选项仅在 ChatGPT mailbox 注册流程适用；OAuth 身份、不使用邮箱 provider、其他平台或用户显式指定注册邮箱时，不走这两类候选路径。

开关关闭时，不复用账号列表中的未完成注册账号或手动无效账号。开关打开时，先选择现有 `lifecycle_status == "failed"` 且仍符合执行器/邮箱服务限制的失败账号；若无可用候选，再追加选择平台为 ChatGPT、`account_origin == "manual"`、`validity_status == "invalid"` 且有密码的记录。两类候选均须由所选主 provider 确认或创建精确地址，继续遵循单任务不重复使用和各自复用次数上限；不得降级/覆盖正常账号。

### 邮箱服务能力边界

通过可选的 provider 能力 `ensure_address(email)` 表达按指定地址准备邮箱的操作，现有 provider 不强制实现。`FallbackMailbox` 只将操作委派给任务所选的主 provider，不得试遍其他 provider。

首期仅为 CF Worker 开放 `ensure_address(email)`：

1. 规范化邮箱为 `strip().lower()`；只有 domain 与当前配置一致、local-part 完全匹配 `^[a-z0-9]+$` 才继续，其他情况不发请求并视为 unsupported。该限制避免服务端静默清理 `.`、`_`、`-`、`+` 或大写字符后创建出别的地址。
2. `GET /admin/address?query=<email>&limit=100&offset=0` 作为存在地址候选查询。响应必须为 2xx JSON 且 `results` 为列表；只按 `result.name.lower() == email` 精确匹配。搜索是子串匹配（LIKE/I-like/instr），所以不能将近似命中算作存在。没有精确命中且 `count > len(results)` 时结果不完整，失败关闭；精确命中则即便还有其他分页结果也可以确认地址存在。
3. 明确不存在时以 `POST /admin/new_address` 发送 `name=local`、`domain=domain`、`enablePrefix=false`。只有 2xx 且返回地址与目标邮箱精确（不区分大小写）相等才成功；服务端改写为其他地址时不重试、不删除。创建非 2xx 后只重查一次；精确命中代表并发下另一请求已创建成功，否则报错。
4. 已存在分支返回该地址、空 token；创建分支返回服务端 JWT。`provider_resource` 形状与普通 CF Worker 创建一致。

空邮件列表不能用来判断地址不存在，因为默认 Worker 设置下，未显式登记的地址也可能收得到邮件。注册凭据使用账号行原有邮箱/密码，服务端 admin token 等配置仍从 provider 配置读取，绝不将 ChatGPT 密码交给邮箱服务。测试只 mock API，不在真实环境调用创建接口。

### 复用流程和失败行为

任务周期开始时：

1. 开关关闭时不查询或认领这两类候选。
2. 开关打开时先查询/认领符合当前执行器条件的注册未完成账号；没有成功准备地址的失败候选后，再查询手动无效候选。
3. 两类候选都交给主 provider `ensure_address`。成功后用原密码与该精确邮箱构造 pinned mailbox，沿现有周期注册、验证码读取和状态更新流程继续。
4. provider 不支持或操作失败时跳过该候选并继续检查同类其他候选/正常新邮箱；日志记录邮箱脱敏标识、provider 和错误类别，不记录 token、密码或服务端凭据。跳过时不得将失败候选计作一次已消耗复用。

成功注册后沿用当前账号生命周期/有效性更新行为，不再把账号标成失败；账号来源元数据需保留。失败的手动候选不应被转换为失败账号，也不得进入现有失败复用池。手动候选列表排除 `lifecycle_status == failed` 的记录，避免与失败路径重复计数。计数键为 `manual_reuse_attempts`，上限复用 `FAILED_ACCOUNT_REUSE_MAX`，用 `patch_account_graph(..., summary_updates=...)` 增量更新 overview，不改 lifecycle/validity。并发领取沿用进程内 `_reused_failed_accounts` + guard，先保留、确认邮箱成功后计数，失败则释放且不计次。

## 备选方案与取舍

- **推荐并采用：** 可选 provider 能力，只接入具有权威存在性判定和 exact-address 创建能力的所选 provider。变更边界清晰，未知 provider 失败关闭。
- **为所有 provider 统一实现：** 当前各邮箱 provider 的地址模型各异，扩大范围并产生不一致的存在性语义，不采用。
- **用邮件列表是否为空判断地址：** 空收件箱不等同于地址不存在，可能造成重复创建，不采用。

## 影响范围

- UI：`frontend/src/pages/Accounts.tsx`（手动新增标记、账号来源徽标、自动注册开关及请求字段）。
- 账号来源持久化：复用 `AccountCreateRequest`/`AccountCreateCommand.overview` 与 account graph overview JSON，目标是避免数据库迁移；测试确认该字段经创建、列表、更新后不丢失。
- 邮箱能力：`core/base_mailbox.py` 的可选方法、`FallbackMailbox` 的主 provider 定向委派，以及首个支持的 provider adapter。
- 任务和查询：`core/db.py` 新增手动无效候选查询/计数更新；`application/tasks.py` 在现有 ChatGPT 周期候选流程追加 gated 分支。
- 测试：账号 API/overview、mailbox adapter/fallback、任务候选门控/失败场景、前端构建或已有前端测试。

## 用户入口文档

README 中英文版的账号池/自动注册说明都应说明：开关默认关闭；仅适用于 ChatGPT 邮箱注册；开启后使用未完成注册账号或手动添加且检测为无效的账号；所选邮箱服务需要支持指定地址确认/创建。CF Worker 邮箱要求小写字母数字 local-part 且域名匹配当前配置，否则该候选会被跳过。

## 验收标准

1. 手动新增账号显示“手动添加”，重启/列表查询后标记仍在；普通导入、自动注册和老账号不会误显示此标记。
2. 注册弹窗开关默认关闭，打开时发送 `extra.reuse_manual_invalid_emails: true`；关闭时不发送或发送 false，旧失败账号复用测试保持通过。
3. 只有来源明确为 manual、有效性明确为 invalid、同平台且有密码的账号成为手动候选；未知来源、正常/未知有效性、无密码记录不候选。
4. 未指定邮箱的 ChatGPT mailbox 任务按主 provider 确认/创建精确地址并复用账号行密码；非目标平台、非 mailbox 身份、显式邮箱及开关关闭均不触发。
5. 按 CF Worker C1 算法处理：仅精确域名/小写字母数字 local-part 可请求；地址查询只接受精确匹配；分页不全、HTTP/JSON错误、冲突重查仍无精确命中、创建后返回地址被改写均不得被当成成功。空邮件列表不得被当成地址不存在。
6. 失败候选跳过不记已消费；成功候选执行遵循现有计数上限及并发保护，不改变有效性/生命周期以外的来源信息。
7. 失败的手动候选不改变生命周期、有效性、来源，也不进入失败复用池；失败生命周期账号只从现有失败路径处理。手动使用计数与失败计数分键，更新不会改写生命周期。
8. 成功和失败路径均保留 `account_origin`；README 中英文文档说明开关及 CF Worker 地址格式限制。
9. 全量相关 Python 测试、前端检查通过；检查日志不含邮箱服务凭据或账号密码。

## 评审记录

Workspace 评审有条件通过（C1–C7 已并入实施计划）：上游 CF Worker 提供存在地址候选搜索和指定地址创建。评审原要求部署版本只读探测；执行期间用户明确要求使用 mock 模拟接口，因此未探测实际部署，真实响应形状仍未验证。评审指出失败路径、候选去重、计数更新、领取顺序、成功来源保留及 README 用户入口均需明确测试/文档，均已补测试或文档。没有真实环境创建调用。
