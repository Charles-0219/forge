# Forge 项目方案

## 基于 BearCode 的可审计 Agent Runtime 与受控 Skill 进化

## 1. 项目定位、目标与范围

### 1.1 项目定位

Forge 是面向个人本地编码任务的 Agent Runtime。

项目基于 BearCode 二次开发，借鉴 OpenHands 的 Conversation、Agent Step、EventLog 和确认策略边界，保留 BearCode 的双协议、工具、MCP、子 agent、长期 Memory、Skills 与在线进化能力。

项目首先服务于作品和面试展示，同时具备执行真实本地任务的能力。

核心目标是形成三个可验证的闭环：

| 能力 | 要回答的问题 | 验证方式 |
|---|---|---|
| 动作可审计 | 模型提出了什么动作，为什么执行或拒绝，结果是什么？ | 动作、审批、观察完整关联 |
| 上下文可重建 | 下一轮模型实际看到了什么，折叠和知识注入改变了什么？ | 事件投影、输入快照、双协议测试 |
| Skill 受控生效 | 用户反馈如何形成经验，候选是否改善了后续任务？ | 人工批准、真实对照评测、版本晋升 |

事件日志负责保存交互事实；执行调度、权限控制和评测分别承担自己的正确性责任。

### 1.2 技术来源与新增贡献

项目文档明确区分三部分：

- **BearCode 原有能力**：双协议调用、工具、权限规则、Memory、Skills、MCP、子 agent、上下文压缩和在线抽取。
- **OpenHands 架构参考**：会话与 agent 分离、持久事件、风险与确认分离、结构化观察和折叠事件。
- **Forge 新增实现**：统一执行入口、事件投影、审批幂等、中断处理、完整上下文注入记录、数据库 Skill 门禁与真实对照评测。

记录参考仓库版本与来源，不将继承能力描述为独立原创成果。

### 1.3 第一版保留能力

- OpenAI-compatible、Anthropic-compatible 调用及流式输出。
- 文件读取、创建、编辑、搜索、Shell 和延迟工具发现。
- Plan Mode、审批、轮数及预算限制。
- stdio MCP。
- 内置和自定义子 agent。
- 项目级、用户级 Skills，支持 inline 与 fork。
- 项目长期 Memory、会话恢复和上下文压缩。
- 在线 Skill 抽取与 add / merge / discard。
- CLI、Web API、控制台和评测报告。

保留功能用途，纠正原有缺陷。子 agent 不再默认绕过权限，Skill 修改不再立即覆盖生效版本。

### 1.4 部署与非目标

第一版限定本机、单用户、单服务进程，默认监听 `127.0.0.1`。同时只执行一个根任务，子任务在根任务内部串行执行。

不引入：

- OpenHands SDK 或 LangGraph 内核。
- 容器沙箱、多租户、用户注册、分布式任务队列。
- 自动重跑中断动作。
- 未验证的 PostgreSQL 兼容承诺。
- 无法从仓库复现的 GAIA/HLE 效果数字。

规则审批不提供操作系统隔离。事件回放可以重建记录与上下文，不保证模型重新调用得到相同输出，也不保证外部动作严格“恰好执行一次”。

## 2. 运行时架构与执行边界

### 2.1 技术栈

- Python 3.11、AsyncIO。
- FastAPI、Uvicorn、Pydantic v2。
- SQLAlchemy 2.0 异步、Alembic、SQLite、aiosqlite。
- OpenAI 与 Anthropic 客户端沿用现有协议分支，锁定依赖版本。
- PyYAML 安全解析 frontmatter，并进行字段类型验证。
- pytest、pytest-asyncio、HTTP 测试客户端。
- 原生 HTML/CSS/JavaScript 单页控制台，使用 fetch 流消费 SSE。

第一版仅验证 SQLite。数据库接口保留适配空间，PostgreSQL 单独作为后续任务。

### 2.2 核心职责

| 子系统 | 职责 |
|---|---|
| Conversation | 接收用户消息，管理会话配置和运行入口 |
| RunCoordinator | 根任务排队、子任务关联、状态、预算、暂停与取消 |
| EventStore | 事务追加事件、分配会话 seq、分页读取 |
| ContextProjector | 从事件生成对应协议的模型上下文，应用折叠 |
| ModelAdapter | 协议转换、流式响应、参数校验、用量与请求错误 |
| ToolDispatcher | 统一执行所有内置工具、MCP、Skill 与子 agent 调用 |
| Policy | 硬约束、风险分类、确认策略和审批决定 |
| MemoryService | 项目记忆存储、检索与实际注入记录 |
| SkillService | 导入候选、版本管理、检索、生效与导出 |
| Evaluator | 固定样本重跑、断言检查、对照报告与晋升判定 |

执行链路：

```text
用户消息
→ 创建 run
→ 构建并记录上下文
→ 模型推理
→ 保存模型响应与动作意图
→ 权限、风险与审批
→ 执行工具
→ 保存观察
→ 继续推理或结束 run
```

`AgentStep` 只完成一次模型推理并产生响应记录。运行循环属于 Conversation，审批等待与工具执行由协调器和 Dispatcher 负责。

原 `_openai_messages`、`_anthropic_messages` 只作为临时请求数据，不再持久化为另一份会话历史。

### 2.3 先修复原执行缺陷

在结构迁移前，建立回归测试并修复：

- OpenAI 多工具调用重复执行。
- 被拒绝动作缺少匹配工具结果。
- Plan 审批缺少 `await`、批准后状态切换错误。
- 子 agent 模型配置透传不完整。
- frontmatter 对引号、布尔值、数组和多行文本解析错误。

迁移过程中继续解决：

- 子 agent 默认 `bypassPermissions`。
- 同步 Shell 阻塞事件循环。
- 工作目录与全局缓存造成会话串扰。
- 工具成功与失败依赖字符串猜测。

### 2.4 统一权限与审批

执行顺序固定为：

```text
工具与参数校验
→ 硬权限和执行前置条件
→ 风险分类
→ 确认策略
→ 执行前复查
→ 工具执行
```

硬约束包括显式 deny、Plan 禁止操作、工具允许列表、读取后编辑校验和子任务权限边界。

硬拒绝不能被 allow 规则、自动确认策略或普通动作批准解除。

读取后编辑使用文件内容哈希。审批通过后再次检查文件状态与权限；状态改变时拒绝该动作并要求模型重新读取和提出动作。

风险默认如下：

| 动作 | 风险 |
|---|---|
| 已注册的读取、列表、搜索 | low |
| 普通文件创建与编辑 | medium |
| 命中危险命令规则的 Shell | high |
| 其他 Shell、缺少风险配置的 MCP 工具 | unknown |

确认策略：

- `always_confirm`：所有通过硬检查的工具动作均需确认。
- `never_confirm`：通过硬检查后自动放行。
- `confirm_risky`：默认策略，确认 high 与 unknown。
- `--dont-ask`：自动拒绝需要确认的动作。
- `--yolo`：映射为 never_confirm，仍受硬约束限制。
- `--accept-edits`：允许普通编辑自动执行，不解除 Shell 与 Skill 门禁。

Plan 批准和 Skill 批准属于独立决策，不能被 never_confirm 代替。

Plan Mode 允许指定计划文件例外；退出计划时提交真实计划正文与内容哈希，批准后才切换执行模式。

审批绑定具体动作与参数，不使用确认文本作为长期授权缓存。重复提交相同决定返回原结果，冲突决定返回 `409`。

### 2.5 工具、子 agent、MCP 与 Memory

**工具执行**

工具返回结构化结果：状态、输出、退出码、错误与附件引用。Dispatcher 不通过输出字符串中的 “Error” 判断成功。

Shell 使用异步子进程，显式传入工作目录，默认超时 30 秒，支持配置与取消。同步文件操作进入受控线程。

第一版工具动作按模型响应顺序执行，先保证正确性；关闭 Anthropic 流式期间提前执行工具的优化。

**子 agent**

内置 explore、plan、general 和自定义定义继续保留。每个子任务建立子会话，关联父 run 与触发动作。

子任务继承模型协议、凭据、工作目录、确认策略与根任务预算；工具权限取父权限和子任务定义的交集。explore、plan 保持只读。

第一版子任务串行 fork-return，禁止嵌套子任务。Skill fork 使用相同机制。

**MCP**

保留 stdio 工具发现与调用。服务配置由用户预先选定，模型不能自行注册新服务。

调用统一经过 Dispatcher。未知风险默认确认；断开、超时、协议错误形成失败观察。持续消费 stderr，关闭服务时清理子进程和未完成请求。

**Memory 与配置**

工作目录通过显式上下文传递，不使用 `os.chdir`。

Memory、Skill、权限、自定义 agent 和 MCP 缓存按目录及配置版本隔离。各模块原有配置优先级分别保留并测试。

Memory 继续按项目隔离存储；写入经过统一权限入口，实际召回内容记录在上下文事件中。系统不自动读取或复制个人历史数据。

## 3. 数据、事件、上下文与公共接口

### 3.1 权威存储

| 数据 | 权威来源 |
|---|---|
| 用户消息、模型响应、动作与观察 | events |
| 调度状态、用量、预算 | runs / actions |
| 审批决定 | approvals，同时追加事件 |
| Skill 内容、版本、active 指针 | skills / skill_versions |
| 长期 Memory | 项目记忆文件 |
| 本轮实际知识注入 | context_prepared 事件 |
| 原始大输出 | 不可变附件，事件保存引用和哈希 |

当前折叠摘要从事件推导，不在 conversations 中保存另一份独立可修改的权威摘要。

### 3.2 表与事件契约

主要表：

- `conversations`：模型配置引用、协议、工作目录、模式、策略和父子关系。
- `events`：会话交互历史。
- `runs`：生命周期、预算、用量与错误。
- `actions`：执行阶段和终态索引。
- `approvals`：动作或计划、决定及决定时间。
- `skills`、`skill_versions`：作用域、版本、证据、状态与 active 指针。
- `eval_reports`：配置、样本、原始输出引用与结果。

事件公共字段：

```text
event_id、conversation_id、seq、run_id、
event_type、schema_version、created_at、payload
```

`event_id` 为 UUID，`seq` 在会话内单调递增，具有数据库唯一约束。

核心事件类型：

```text
user_message
context_prepared
assistant_message
action
action_started
observation
action_rejected
approval_decided
condensation
run_state_changed
mode_changed
skill_proposed
skill_decided
skill_eval_completed
skill_promoted
```

动作关联 `action_id`、`model_call_id`、模型 `tool_call_id` 与调用顺序。观察或拒绝必须匹配具体动作；每个动作只允许一个终态。

父子会话分别拥有 seq，控制台通过会话 ID 与 seq 标识事件，不伪造跨会话全局顺序。

### 3.3 上下文投影

每次模型请求前，`context_prepared` 保存：

- 实际系统提示。
- 实际注入的 Memory 内容。
- 实际注入的 Skill 内容与版本。
- 本轮工具定义快照。
- 模型参数、协议和投影截止位置。
- 投影输入哈希。

不存模型密钥，也不另存完整、可恢复的消息数组。

Projector 使用记录中的内容，不在重建时重新检索磁盘。协议专有内容按原协议保留；已有会话不支持切换协议。

拒绝投影明确表示“没有执行”。Anthropic 设置 `is_error`，OpenAI 返回匹配调用 ID 的失败内容。

模型响应包含多个工具调用时，所有调用均获得结果、拒绝或未知结果说明，保持协议配对完整。

### 3.4 折叠

只折叠连续、完整、已结束的交互段，保留最近一个完整用户交互轮次以及所有未完成动作。

折叠事件记录：

- 被替代的事件 ID。
- `episode_memory`、`working_memory`、`tool_memory`。
- 摘要生成配置。
- 被替代的旧折叠事件 ID。

原始日志保留。Projector 应用当前有效摘要，避免旧摘要重复注入。折叠在工具结果配对完整的安全点执行。

工具输出截断、旧结果清理和微压缩统一为可版本化的投影规则，不修改历史事件正文。

### 3.5 事务与中断处理

用户消息与 run 创建在同一事务中完成。动作阶段更新、审批决定、运行状态变化和 Skill 晋升均与对应事件原子提交。

动作阶段：

```text
proposed / waiting_approval / approved / executing /
finished / rejected / outcome_unknown
```

工具执行前，提交 executing 状态与 action_started；执行后保存观察和终态。

run 状态：

```text
queued / running / waiting_approval / paused /
succeeded / failed / cancelled / interrupted
```

暂停在当前动作完成后的安全点生效。取消尽力停止本地进程与子任务，不能确定外部副作用时记录未知结果。

服务启动时：

- 遗留非终态 run 标为 interrupted。
- 未决审批标为过期。
- 未完成动作补齐拒绝或未知结果。
- 不自动恢复执行、不自动重跑。

`--resume` 恢复会话和上下文；新消息创建新 run。`/clear` 创建新会话，旧记录保留。

### 3.6 API、SSE 与客户端

服务端使用配置中的 API 密钥，通过 `X-API-Key` 校验。模型密钥只保留在服务端。

接口前缀 `/v1`：

| 接口 | 用途 |
|---|---|
| `POST /conversations` | 创建会话 |
| `GET /conversations/{id}` | 查询配置摘要、状态及父子关系 |
| `POST /conversations/{id}/messages` | 追加消息并创建 run |
| `GET /conversations/{id}/events?after_seq=&limit=` | 分页历史 |
| `GET /conversations/{id}/events/stream?after_seq=` | 回放与实时事件 |
| `GET /runs/{id}` | 状态、用量、预算与错误 |
| `POST /runs/{id}/pause`、`resume`、`cancel` | 运行控制 |
| `POST /runs/{id}/approvals/{approval_id}` | 动作或计划审批 |
| `POST /conversations/{id}/compact` | 请求安全点折叠 |
| `POST /conversations/{id}/mode` | 空闲时切换 Plan 状态 |
| `GET /skills`、`POST /skills/{id}/decision` | 查看与决定候选版本 |
| `GET /memories?conversation_id=` | Memory 列表 |
| `POST /conversations/{id}/skill-extractions` | 显式抽取候选 |
| `GET /evals/latest`、`GET /evals/{id}` | 评测报告 |

消息提交携带客户端请求 ID，重复提交返回原 run。同会话已有未完成 run 时返回 `409`；不同会话任务排队执行。

SSE 持久事件 id 使用 seq。重连支持 `Last-Event-ID`，显式 `after_seq` 优先。服务器始终按数据库游标读取，通知只负责唤醒，每秒进行补偿检查。

流式 token 使用无持久游标的 `token_delta`，关联模型调用 ID；最终响应替换临时文本。控制台通过 fetch 流携带 API 密钥。

CLI 只调用服务端，不保存第二份消息历史。保留主要命令用途，并明确新版权限语义。

## 4. Skill 门禁与可复现评测

### 4.1 Skill 生命周期

```text
证据积累
→ proposed
→ approved
→ evaluating
→ active / eval_failed
```

人工拒绝为 rejected；新版本晋升后旧 active 为 superseded。

自动提案必须引用两个不同用户反馈事件，或一条明确的长期偏好指令。助手输出、工具结果和检索内容不能计入用户证据。

模型负责抽取和归并建议；程序检查证据来源与数量，审批者确认语义依据。一次性参数、秘密和项目事实过滤或进入 Memory。

显式要求创建或修改 Skill 可以生成候选，但不能跳过批准和评测。

add、merge、skill_create、skill_evolve 均只创建新候选版本，不直接修改 active。

### 4.2 版本冻结与生效

批准绑定候选内容哈希和固定评测套件。修改候选必须产生新版本并重新批准。

无适用评测套件时保持 approved，不能自动晋升。

检索、inline 和 fork 执行只读取数据库 active 版本。用户级与项目级 Skill 保留既有优先级。

文件发现保留，但外部文件变化只形成导入候选。内置演示 Skill 通过明确的初始化导入成为基线，并记录来源与版本。

SKILL.md 是数据库版本的导出物，采用临时文件与原子替换；同步失败记录状态并支持重试，不影响数据库 active 版本。

### 4.3 三套评测

**A. 运行时正确性**

使用模拟模型、临时目录和本地 MCP 测试服务验证：

- 工具执行次数和协议配对。
- 硬约束与审批幂等。
- 子任务权限和预算。
- 折叠、恢复与 SSE。
- Skill 版本门禁。

此套必须全部通过。

**B. Skill 检索**

固定 20 条人工标注查询，包含应命中和不应命中样本。

报告 Recall@3、首位命中率、无关注入数，并保存金标版本、检索配置和逐条结果。

**C. Skill 真实效果**

第一套演示评测固定 12 条开发样本、8 条留出样本，按任务家族分组，禁止简单改写跨集合泄漏。

分别真实重跑：

1. 不注入目标 Skill。
2. 使用当前 active 版本。
3. 使用候选版本。

其余 Skill、Memory、工具、模型和参数保持一致。评测在独立会话和临时工作目录中执行，不写回正常 Memory，也不继续触发在线进化。

断言由人工预先定义。留出内容不进入抽取、候选改写或开发提示。

晋升条件：

- 候选全部硬断言通过。
- 留出任务通过数高于当前 active；首次晋升高于无 Skill 基线。
- 无新增权限违规或泄密失败。

平分不自动晋升。调用失败或评测不完整不晋升。报告保存代码、模型、Skill 与样本版本，以及原始输出、逐项断言、token 和耗时。

费用仅在定价配置明确时报告。小样本结果展示通过数与总数，不以百分比暗示统计稳定性。

## 5. 实施、验收与交付

### 5.1 分阶段实施

| 阶段 | 工作 | 进入下一阶段的条件 |
|---|---|---|
| 1. 干净底座 | 复制运行时，清除秘密与个人数据，修复执行和解析缺陷 | 原缺陷回归测试通过 |
| 2. 事件内核 | 双协议 Adapter、内存 EventStore、Projector，再接 SQLite | 多工具、拒绝和上下文投影测试通过 |
| 3. 统一执行 | 硬约束、审批、异步工具、暂停取消与中断处理 | 幂等、崩溃窗口和事件循环响应测试通过 |
| 4. 全能力迁移 | MCP、子 agent、Memory、配置缓存和根预算 | 无绕过 Dispatcher 的执行路径 |
| 5. 受控进化 | 版本门禁、真实评测、active 检索及导出 | 三套评测可运行，晋升与失败路径均验证 |
| 6. 客户端与展示 | CLI、API、控制台、说明与讲稿 | 黄金路径和故障演示可复现 |

### 5.2 必须通过的验收场景

- 两个工具各执行一次，各有且仅有一个终态结果。
- 单个及批量拒绝均形成合法双协议消息。
- allow、never_confirm 和子 agent 均不能解除硬拒绝。
- 重复批准不重复执行，审批后文件变化会阻止执行。
- 已记录上下文不受之后磁盘 Skill、Memory 变化影响。
- 折叠不产生孤立调用、重复摘要或丢失未决动作。
- Shell 执行时，状态查询、审批和 SSE 仍可响应。
- 子任务正确继承模型配置、权限和预算。
- MCP 超时、断开和错误均形成失败观察。
- SSE 经重连和通知丢失后，按 seq 去重仍能取得完整持久事件。
- 重启后不重跑未知动作，旧审批无法继续批准。
- 未批准、未通过评测的 Skill 不进入正常检索和执行。
- 导出失败不导致数据库与运行时 active 版本分叉。
- 留出样本不进入候选开发输入。

### 5.3 控制台与演示

单页控制台提供：

1. 事件时间线：动作、观察、折叠、运行状态和子任务。
2. 审批视图：具体参数、风险、原因、计划正文与决定。
3. Skill 与评测视图：证据、版本差异、基线对照和晋升结果。

黄金路径：

```text
执行任务
→ 用户纠正并明确长期约束
→ 形成候选
→ 人工批准冻结版本
→ 真实对照重跑
→ 留出门禁通过后晋升
→ 后续任务使用对应 active 版本
```

另展示一次动作拒绝、一次折叠和一次中断恢复。评测未通过时展示真实失败原因，不预设候选一定晋升。

### 5.4 交付物与成果表述

交付运行时代码、数据库迁移、测试、配置示例、API 契约、人工评测样本、完整报告、架构决策、项目说明与演示脚本。

README 和面试材料中的数字必须关联评测报告、样本数与版本。

可以主张的工程成果包括：统一动作执行边界、双协议事件投影、审批幂等、中断记录和受控 Skill 晋升。

“提升任务效果”必须由真实对照支持；规则覆盖率不能描述为整体安全保证，日志恢复不能描述为外部动作严格恰好执行一次。

**默认选择：完整保留 BearCode 能力，采用本地单进程与串行根任务，优先验证执行正确性，再用真实留出评测决定 Skill 是否生效。**
