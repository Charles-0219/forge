# Forge 第一阶段：底座核验报告

## 结论与范围

第一阶段门禁通过：方案列出的五类执行/解析缺陷均有修复前失败证据，修复后全部回归通过。没有实施阶段 2–6；尚不能将本底座当成完成统一权限隔离的 Forge 服务。

## 来源与清理

- 原仓库：`/Users/fuqinghua/Downloads/源码+简历+测评+全量文档/BearCode`。
- 基线 commit：`9573e652bdd39ebd642da76be1f9f94171ffdece`，提交日期 2026-09-05。
- 原分支 `main`，跟踪 `origin/main`；开始与结束均只有 `.DS_Store` 的既有修改。本任务未修改原仓库。
- 导入已提交的 15 个 `agents/*.py` 与 `requirements.txt`，共 16 个文件；基线/current SHA-256 见 `baseline.json`。
- 导入时以 Git commit 文件白名单取源，排除真实 `.env`、`.mcp.json`、`.bear`、会话、Memory、演化记录、IDE 状态、数据集、网页/打包产物和 Git 历史。未读取或复制个人历史数据。
- 原仓库潜在秘密配置位置：`.env`、`.mcp.json`。仅核实位置，没有读取内容，故不判断其是否实际包含秘密。
- 对导入运行时与测试进行常见密钥/私钥模式的文件名级扫描，没有命中。该检查不是秘密不存在的证明。
- 原仓库未发现根 runtime LICENSE，公开分发/发布需补齐上游许可依据。

## 确认的缺陷与修复

| 待核验项 | 证据与原因 | 修复结果 |
|---|---|---|
| OpenAI 多工具重复执行 | 批次执行错误地位于权限预检循环内，前面调用反复执行 | 预检与执行分离，读工具与写工具每调用只执行一次 |
| 被拒绝动作缺少配对结果 | 全拒绝/尾部拒绝不能完成批次输出；预算退出也遗留无结果调用 | 每个拒绝/预算跳过调用输出匹配 result；Anthropic 拒绝加错误标记 |
| Plan 审批与状态切换 | async 回调没有 await；传路径而非正文；目标模式被写入又清空，permission_mode 未更新 | await 正文回调，4 种选择与清理历史回归通过；缺少文件/回调/无效选择不批准 |
| 子 agent 配置透传 | 普通子代理与 Skill fork 仅透传 model/OpenAI URL | 透传有效 key、协议专用 URL 与 thinking；两协议、两入口回归通过 |
| frontmatter | 原手写 parser 把引号/数组/bool 当字符串，多行丢失；格式化不能正确 roundtrip | safe YAML、消费者字段校验；保留旧逗号列表和 bool 字符串兼容 |

相关范围内补充修复：adaptive thinking 方法名拼写；`custom_tools=[]`、自定义 agent/Skill fork 空白名单意外开放工具；共享解析器切换后 Memory 非文本字段未校验。

修改的导入文件只有：`agents/agent.py`、`agents/frontmatter.py`、`agents/memory.py`、`agents/skills.py`、`agents/subagent.py`、`requirements.txt`。没有进行数据库、事件调度、权限框架或 Skill 晋升重构。

新增交付：`AGENTS.md`、`TASKS.md`、兼容清单、来源哈希、README、测试配置、依赖锁、6 个测试模块与隔离夹具、哈希校验脚本、JUnit 证据。`.gov/` 保留编辑前快照、修改日志与目录索引。

## 测试证据

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 未修复原始导入代码，首批回归 | 75 项：55 failed、20 passed；Plan 未 await 产生警告 | `artifacts/phase1-baseline.xml` |
| 共享解析调用方/空白名单补充回归，修复前 | 6 项：5 failed、1 passed | `artifacts/phase1-consumers-before.xml` |
| 全部修复后 | 81 passed，无 warnings | `artifacts/phase1-final.xml` |
| Python 编译检查 | agents/tests/scripts 通过 | `python -m compileall -q agents tests scripts` |
| 依赖一致性 | 28 packages，兼容通过 | `uv pip check --python .venv/bin/python` |
| 来源/current 哈希 | 16 个文件验证通过 | `python scripts/verify_baseline.py` |

回归覆盖双协议模拟响应、多工具执行次数、允许/拒绝/确认拒绝组合、预算跳过配对、Plan 4 选择、子代理配置、YAML 类型及 roundtrip、空工具列表、配置优先级和 Memory 合成项目隔离。

测试使用临时 HOME/cwd，清除 API 配置环境、替换 session 路径、禁 socket 连接、禁自动 Skill 演化；没有真实模型调用和用户 MCP 启动。修复过程中一次缩进错误和补充测试夹具错误已纠正；报告中的失败证据来自正确执行的行为回归。

环境：Python 3.11.16，macOS arm64；`requirements-dev.lock` 固定全部已安装依赖版本。该文件是版本锁，不是供应链制品哈希锁；其他平台尚未验证。

## 未解决问题

| 问题 | 影响/安排 |
|---|---|
| 子代理默认 bypass、权限 allow/bypass 先于 plan 硬约束 | 当前底座不具备 Forge 权限承诺；阶段 3–4 消除旁路 |
| 同步 Shell、隐式 cwd、进程全局缓存 | 阻塞与会话串扰；阶段 3–4 迁移 |
| 结果靠字符串猜测、文件前置条件靠 mtime | 缺少可靠动作终态与文件复查；阶段 3 修复 |
| Anthropic 提前执行、取消/异常/上下文重置边界 | 本阶段保留原机制；事件投影和 Dispatcher 迁移时建立完整测试 |
| Plan CLI toggle 与审批期间文件变化 | 尚无统一审批对象和内容哈希绑定；阶段 3 设计，不宣称完整防绕过 |
| 子任务嵌套与共享预算 | 尚未实现根预算硬约束；阶段 4 迁移 |
| Skill 自动提取直接写活动文件 | 未具备评测与晋升门禁；阶段 5 处理，核验测试禁自动演化 |
| 真供应商/MCP/CLI/历史文件兼容 | 未实测；使用合成数据，未访问个人历史 |
| 上游许可证与项目 Git 快照 | 本地私有核验已完成；发布许可待确认，本工作目录尚未建立 Git 仓库 |

## 下一阶段前置条件

1. 审阅本报告与兼容清单，明确授权“阶段 2：事件内核”；当前不自动启动。
2. 建立独立项目 Git 仓库并提交本阶段代码、测试、依赖锁和基线清单，形成后续可回滚的开发起点。
3. 固定 Adapter/Event/EventStore/Projector 的契约，明确动作 ID 与双协议结果配对、事务提交边界和 SQLite 迁移策略；此时才开始实现。
4. 使用合成历史与临时数据库验证投影和迁移，继续禁止导入个人历史/真实秘密。
5. 阶段 2 验收要求：双协议投影一致、内存与 SQLite EventStore 行为一致、事件与业务状态原子提交，并保持本阶段回归全绿。
6. 若要公开发布或分发源码，先确认上游授权/许可证；阶段 2 本地开发不需要以公开发布为前提。
