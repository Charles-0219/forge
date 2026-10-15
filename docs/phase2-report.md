# Forge 第二阶段：事件内核报告

## 结论与范围

阶段 2 门禁通过。模拟的 OpenAI 与 Anthropic 响应可以记成事件，内存库和 SQLite 的行为一致，投影只使用已经写入的上下文。

本阶段没有执行 Shell 或文件工具，没有调用 DeepSeek，没有启动 MCP，也没有修改 `agents/*.py`。子代理权限、同步 Shell、全局缓存和 Skill 晋升仍留在后续阶段。

## 交付

- `forge/`：适配器、投影器、内核、内存事件库、SQLite 事件库。
- `alembic/versions/0001_phase2_event_kernel.py`：`conversations`、`events`、`runs`、`actions`、`approvals`。`approvals` 只有表，没有写入路径。
- 依赖在 `requirements.txt` 和 `requirements-dev.lock` 中追加 SQLAlchemy 2.0.54、Alembic 1.20.0、aiosqlite 0.22.1、greenlet 3.5.6、Mako 1.4.3、MarkupSafe 3.0.4。阶段 1 已有包的版本没有重选。

数据库文件由调用方传入路径。测试使用临时文件。内核不会把数据写到 `~/.bear`、`~/.BearCode` 或 BearCode 的 `data/`。

## 已验证的行为

| 检查 | 结果 |
|---|---|
| 两个工具各有一个终态，投影后的调用 ID 一一对应 | 通过 |
| 拒绝、预算跳过和未知结果保持配对；Anthropic 设置 `is_error` | 通过 |
| 非 function 的 OpenAI 调用立即得到拒绝结果 | 通过 |
| 重复终态被拒绝 | 通过 |
| `context_prepared` 之后磁盘内容变化，重建哈希不变，密钥不进事件 | 通过 |
| 相同输入的系统提示和输入哈希稳定 | 通过 |
| 相同客户端请求 ID 返回原 run；正文不同或已有未结束 run 时冲突 | 通过 |
| 写入中途失败后，消息和动作都不会留下 | 通过 |
| 内存库与 SQLite 的事件类型、载荷和投影结果一致 | 通过 |
| 迁移后的列与 SQLAlchemy 模型一致，审批表没有记录 | 通过 |
| 阶段 1 运行时哈希 | 15 个 `agents/*.py` 仍等于阶段 1 哈希 |
| 全量测试 | 108 passed |
| 评测样本冻结哈希 | 通过 |

命令：

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q agents forge tests scripts alembic
.venv/bin/python scripts/verify_baseline.py
.venv/bin/python scripts/verify_eval_freeze.py
.venv/bin/python -m pip check
```

环境仍是 Python 3.11.16、macOS arm64。测试使用合成响应和临时目录，没有网络调用。

## 仍未解决

- `agents/` 里的 Anthropic 提前执行、子代理 `bypassPermissions`、同步 Shell、mtime 编辑检查、字符串成败判断和全局缓存都还在。
- 阶段 2 不把事件内核接进 `Agent.chat`。
- 折叠只保留事件类型，没有实现替换历史的投影策略。
- 审批表未使用。暂停、取消、崩溃恢复留到阶段 3。
- 真实 `deepseek-flash` 调用留到阶段 5，并受 15 美元上限约束。
