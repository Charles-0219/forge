# Forge 第四阶段：子任务、MCP 与隔离报告

## 结论与范围

阶段 4 门禁通过。子任务、预先配置的 stdio MCP、Memory 和配置缓存都进入阶段 3 的执行路径。`Coordinator` 仍没有接进 `Agent.chat`。

本阶段没有调用 DeepSeek，没有读取留出样本，没有读取个人会话、Memory、真实 `.env` 或真实 MCP 配置。测试里的 MCP 进程是本地假服务，不打开网络端口。

## 交付

- `forge/children.py`：explore、plan、general、custom 和 skill fork 串行返回。子会话记录父会话、根 run 和触发动作。权限是父白名单与子定义的交集；explore 和 plan 再限制为只读。嵌套子任务被拒绝。模型调用计入根预算，子任务没有额外配额。用量不完整时标记 `usage_unknown`，不按缺失的 token 数执行上限。凭证引用只留在进程内存。
- `agents/agent.py`：两处子代理构造不再使用 `bypassPermissions`。其余 `agents/*.py` 未改。
- `forge/mcp.py`：只加载调用方传入的 stdio 配置。模型不能登记服务。发现结果受当前白名单过滤，调用经过 `Coordinator`。超时、断开和协议错误成为失败观察。stderr 持续读取。关闭时结束进程。
- `forge/isolation.py`：缓存按目录和配置版本保存在实例里。Memory 写在对应项目的 `.forge/memory/` 下，未获允许时不创建文件。API 地址、密钥、`.env`、Skill、agent 和 MCP 的优先级与阶段 1 一致，解析时只使用传入的字典。
- `scripts/verify_baseline.py`：带 `current_sha256` 的 `agents/` 文件可以和阶段 1 哈希不同。目前只有 `agents/agent.py` 使用这个字段。

## 已验证的行为

| 检查 | 结果 |
|---|---|
| 子会话继承协议、模型、工作目录、确认策略和 thinking 标记；凭证引用不进入事件或 run 预算 | 通过 |
| explore、plan 和空白名单不能写文件或执行 shell；父子白名单取交集 | 通过 |
| 嵌套子任务被拒绝；前一个子任务未结束时不能再派发；结束后可以 | 通过 |
| 根预算 `max_calls` 用尽后拒绝下一次调用；`usage_unknown` 不套用 token 上限 | 通过 |
| MCP 成功、超时、断开、协议错误、stderr 和关闭清理 | 通过 |
| 白名单之外的 MCP 工具不可见且不会执行；Plan 模式拒绝 MCP；`confirm_risky` 先审批 | 通过 |
| 两个缓存实例和两个目录互不可见；拒绝写入时不创建 Memory 文件 | 通过 |
| `forge/` 里没有 `os.chdir` 和 `bypassPermissions`；子进程只出现在 dispatcher 和 MCP | 通过 |
| 阶段 1–3 测试与评测冻结 | 通过 |
| 全量测试 | 158 passed |

命令：

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q agents forge tests scripts alembic
.venv/bin/python scripts/verify_baseline.py
.venv/bin/python scripts/verify_eval_freeze.py
.venv/bin/python -m pip check
```

环境仍是 Python 3.11.16、macOS arm64。测试使用合成响应、临时目录和本地假 MCP 服务，没有网络调用。

## 仍未解决

- `agents/tools.py`、`agents/mcp_client.py` 和 `agents/prompt.py` 里的旧子进程还在。`agents/main.py` 仍把 `--yolo` 映射为 `bypassPermissions`。这些属于旧 CLI 循环，留到阶段 6。
- 新执行器没有接进 `Agent.chat`。
- Shell 和 MCP 子进程都不是操作系统沙箱。未配置的外部命令仍可能访问本机。
- 用量不完整时只标记未知，不声称已经卡住 token 上限。
- Skill 文件写入仍在旧的 `agents/skills.py`。阶段 4 的 skill fork 只执行，不修改 Skill。
- 真实 `deepseek-flash` 调用留到阶段 5，并受 15 美元上限约束。
