# BearCode → Forge 兼容清单（第一阶段）

基线：`9573e652bdd39ebd642da76be1f9f94171ffdece`。源码核验与合成回归是本清单的证据，未进行真实供应商或 MCP 服务调用。项目方案/spec 是需求参考，其中后续阶段要求不构成本次实施授权。

## 配置与能力

| 项目 | 核实的基线行为 | 第一阶段处理/证据 |
|---|---|---|
| 模型协议 | 提供 OpenAI Chat Completions 与 Anthropic Messages 两条路径；URL 路径包含 `/anthropic` 时选 Anthropic | 保留两协议，模拟流式响应回归通过；真实服务兼容未验证 |
| API URL | CLI 参数 > API > MINI_CLAUDE_API_BASE > OPENAI_BASE_URL > ANTHROPIC_BASE_URL | 保留；`test_api_configuration_precedence` |
| API key | APIKEY > MINI_CLAUDE_API_KEY；否则优先所选协议 key，再回退另一协议 key | 保留；仅用虚构 key 测试 |
| `.env` | 进程环境优先于 `.env`，`override=False` | 保留；`test_dotenv_preserves_existing_environment` |
| Skills | `~/.bear/skills/<name>/SKILL.md` 优先于项目 `.bear/skills` 同名 Skill | 保留用户级优先；后续阶段重新设计显式作用域 |
| 自定义 agent | 项目 `.bear/agents/*.md` 覆盖用户级同名定义 | 保留项目级优先；合成同名定义回归通过 |
| 工具白名单 | `allowed-tools` 缺省开放默认工具；基线空列表也意外扩大权限 | 纠正：空列表保留为空；数组及逗号列表均支持 |
| 权限规则 | 用户与项目 allow/deny 累积；常规路径 deny 优先，但 bypass 模式在规则检查前放行 | 保留配置合并；不宣称完成硬权限约束 |
| MCP 配置 | 用户 settings → 项目 settings → 项目 `.mcp.json`，后者替换同名服务 | 保留；仅 `_load_configs` 回归，不启动服务 |
| Memory | `~/.BearCode/projects/<cwd 的 SHA256 前16位>/memory` | 合成项目隔离、读写 roundtrip 通过；实际个人目录未读取 |
| 会话 | `~/.bear-code/sessions`，折叠记忆另存项目 `.bear/sessions` | 源码静态核验；数据未迁移，测试替换 SESSION_DIR |
| Shell | 同步 `subprocess.run`，默认超时 30000ms，依赖进程 cwd | 本阶段保留；阶段 3 改异步生命周期 |
| 文件编辑 | read-before-edit 依赖 mtime 校验 | 本阶段保留；阶段 3 增加内容哈希/执行前复查 |
| 结果类型 | 成功/失败仍用字符串启发式判断 | 本阶段保留；阶段 3 改结构化结果 |
| 缓存 | Skill、agent、权限与工具激活状态采用进程全局缓存 | 测试主动重置；阶段 4 做显式隔离 |
| 子 agent | fork-return；非 plan 默认 bypassPermissions，根预算未共享 | 修复 model/key/协议 URL/thinking 透传；权限与共享预算留阶段 4 |
| Plan | async 审批、4 类选择；基线没 await、传文件路径、没有真实状态切换 | 修复正文读取/await/模式更新；无回调/无文件/未知选择保持 plan |
| YAML frontmatter | 原为简易逐行 `key:value`，无法可靠处理引号、bool、数组、多行 | 改 safe_load/safe_dump，消费方验证类型，非法 Skill/agent/Memory 不加载 |
| Skill 演化 | 在线提取默认开启，写入活动文件；缺少数据库版本与评测晋升门禁 | 保留源码行为，测试关闭自动演化；阶段 5 迁移 |
| CLI/展示 | 原 argparse CLI、rich 展示与文件会话 | 仅导入运行时，不搬运 UI、网页、打包产物与个人配置 |

## 明确的行为修正

1. OpenAI 权限预检后只执行一次完整工具批次；拒绝/取消确认也得到匹配结果。
2. OpenAI 达到预算时对尚未执行的调用补齐 skipped 结果；Anthropic 拒绝结果设置 `is_error=true`。
3. Plan 的执行选择改为 `acceptEdits`；manual-execute 恢复前一模式；keep-planning 保持原状态；clear-and-execute 清理历史。
4. 子代理与 Skill fork 使用父代理有效 SDK URL/key，并传递 thinking；修正 adaptive thinking 方法名拼写。
5. YAML 原生类型保留；文本、布尔和工具白名单校验；空白名单不扩大权限。旧字符串 `"true"`/`"false"`、逗号工具列表继续接受。

## 未覆盖的兼容面

- 真实流式 API、第三方网关、断网重试与供应商模型能力，尚无实测。
- MCP 初始化/传输/服务进程生命周期、CLI 交互与恢复未实测。
- 历史 Memory/Skill/会话没有读取、采样或迁移；兼容性依据源码与合成文件。
- YAML 按标准解析：此前不规范的裸值可能变成非字符串并被校验拒绝；应修正配置，不能静默扩大权限。
- `toggle_plan_mode` 和权限规则优先级仍存在绕过审批/plan 约束的路径；当前修复不等于统一安全执行器。
- Anthropic 流式期间提前执行工具仍保留，未来结构迁移按 spec 关闭；取消、上下文重置、执行异常后的完整终态配对未在本阶段全覆盖。
- 阶段 1 核验时，原 BearCode 检出根目录没有 runtime LICENSE。Forge 仓库随后由所有者以 MIT 许可公开。

## 阶段 2 事件内核

阶段 2 没有修改 `agents/*.py`。原循环的权限、Shell、子代理和 Skill 行为保持阶段 1 的状态。

新增 `forge/` 只接受模拟的 OpenAI / Anthropic 响应，写入事件并投影。真实模型、MCP 和工具执行仍然没有接入。Anthropic 预算跳过在新投影里设置 `is_error`；旧循环里的对应缺口留到阶段 3。
