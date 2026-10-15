# Forge 第三阶段：统一执行报告

## 结论与范围

阶段 3 门禁通过。策略、审批、六个内置工具、运行生命周期、崩溃恢复和折叠都在 `forge/` 里完成。`agents/*.py` 没有修改，也没有接进 `Agent.chat`。

本阶段没有调用 DeepSeek，没有启动 MCP，没有写 Skill，也没有创建子代理。Shell 只固定工作目录并结束子进程，不是操作系统隔离。

## 交付

- `forge/policy.py`：参数校验、显式拒绝、模式、白名单交集、读后内容哈希、预算和取消。allow、`never_confirm`、`--yolo` 和普通批准不能解除硬拒绝。`--yolo` 映射为 `never_confirm`。`--accept-edits` 只自动确认普通编辑。
- `forge/dispatcher.py`：`read_file`、`list_files`、`grep_search`、`write_file`、`edit_file`、`run_shell`。结果用 `success`、`error`、`outcome_unknown`。Shell 是异步子进程，默认超时 30 秒。文件操作在线程里执行。
- `forge/coordinator.py`：审批、暂停、取消、恢复、计划批准和折叠入口。重新打开数据库后要先调用 `recover()`，再 `dispatch()`。
- `forge/fold.py`：只替换已经结束的交互段。投影使用最新一份摘要，原始事件不改。
- `alembic/versions/0002_phase3_approvals.py`：审批表增加参数哈希、参数、状态和依据。

阶段 2 的 `settle_action` 仍可直接把 `proposed` 记成终态，供原有配对测试使用。它不执行工具。阶段 3 的执行从 `executing` 和 `action_started` 开始。

## 已验证的行为

| 检查 | 结果 |
|---|---|
| allow、自动确认和已确认状态不能解除硬拒绝；Plan 下的 Shell 被拒绝 | 通过 |
| 重复批准不第二次执行；相反决定 409；过期审批 410 | 通过 |
| 批准后文件变化、计划正文变化、符号链接或路径逃逸阻止执行 | 通过 |
| 长时间 Shell 期间，状态查询和另一项审批仍能返回；超时会结束进程 | 通过 |
| 执行前崩溃、执行后观察提交前崩溃，恢复后不自动重跑工具 | 通过 |
| 折叠不留下孤立调用，不重复摘要，未决动作仍在 | 通过 |
| 父子白名单交集拒绝交集之外的工具 | 通过 |
| 暂停后的批准不会执行；终态 run 不能恢复 | 通过 |
| 阶段 1 运行时哈希 | 15 个 `agents/*.py` 仍等于阶段 1 哈希 |
| 全量测试 | 141 passed |

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

- `agents/` 里的旧循环、子代理 `bypassPermissions`、同步 Shell、字符串成败判断和全局缓存都还在。阶段 4 再拆。
- 执行器没有接进 `Agent.chat`。
- 父子权限只留了交集接口，子任务本身不在本阶段。
- Shell 没有沙箱。工作目录和进程组之外的命令仍可能访问本机。
- 真实 `deepseek-flash` 调用留到阶段 5，并受 15 美元上限约束。
