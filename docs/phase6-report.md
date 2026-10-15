# Forge 第六阶段：本机客户端报告

## 结论与范围

阶段 6 的本机 API、SSE、命令行客户端、控制台和演示已经通过测试。全量测试 **182 passed**。回归没有调用 DeepSeek，也没有监听除 `127.0.0.1` 以外的地址。

`agents/*.py` 没有改。旧循环继续承担阶段 1 回归，它不是新的客户端。

## 交付

- `forge/api.py`：`/v1` 接口。`X-API-Key` 不匹配返回 401，响应里不带回密钥。参数错误返回 422，且不回显提交值。会话、消息、事件分页、SSE、run 控制、审批、折叠、进入 Plan、Skill 列表和决定、显式抽取、Memory、评测和附件都在这里。
- 相同 `client_request_id` 加相同正文返回原来的 run。正文不同，或会话仍有未结束的 run，返回 409。
- SSE 用会话 seq 作为持久事件 id。查询参数 `after_seq` 优先于 `Last-Event-ID`。数据库游标负责补齐，通知只负责提前唤醒。
- `forge/cli.py`：`clear` 新建会话，`resume` 选择旧会话，`send` 提交消息。`serve` 只接受 `127.0.0.1`。
- `forge/static/console.html`：密钥放在页面内存里，用 `textContent` 显示接口返回的文本。
- `scripts/demo_phase6.py`：模拟模型下的写入拒绝、折叠，以及恢复后旧审批返回 410。
- `docs/openapi.json`：由应用生成的契约。

## 仍未解决

- 旧的 `agents/main.py` 仍自己保存消息历史。`--yolo` 在那里仍映射为 `bypassPermissions`。`agents/tools.py`、`agents/mcp_client.py` 和 `agents/prompt.py` 里的子进程还在。
- 新执行器和 Skill 门禁没有接进 `Agent.chat`。
- 真实 C 套没有启动。手动命令仍是 `scripts/run_live_eval.py --confirm-live`。
- Shell 和 MCP 子进程不是操作系统沙箱。
- 演示里的模型响应是模拟的。候选晋升没有用真实留出样本跑完。
