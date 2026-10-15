# Forge：第一阶段底座

本目录是 BearCode 运行时的干净导入。阶段 1 到阶段 6 已在本地实现并通过测试。真实模型调用仍要单独启动。

许可证是 MIT。公开仓库是 <https://github.com/Charles-0219/forge>。权利与历史基线见 `PROVENANCE.md`。已批准的评测样本见 `evals/`。

阶段 2 增加了 `forge/` 事件内核。它记录会话、动作和双协议投影，并把同一套行为落到内存或 SQLite。

阶段 3 在同一内核上增加策略、审批、六个内置工具、恢复和折叠。执行入口是 `Coordinator`，不替换 `agents/` 里的原循环。结果见 `docs/phase3-report.md`。

阶段 4 把子任务、预先配置的 stdio MCP、Memory 和配置缓存放进同一条执行路径。子代理不再用 `bypassPermissions` 创建。结果见 `docs/phase4-report.md`。

阶段 5 把 Skill 放进数据库门禁。只有 active 版本能被检索或 fork。候选要先批准，再由手动评测决定是否晋升。结果见 `docs/phase5-report.md`。

阶段 6 增加本机 HTTP API、SSE、命令行客户端和静态控制台。模型密钥不进入接口响应。结果见 `docs/phase6-report.md`。

## 来源与交付

- 基线提交：`9573e652bdd39ebd642da76be1f9f94171ffdece`。
- 开发约束：`AGENTS.md`；阶段进度：`TASKS.md`。
- 核验结果：`docs/phase1-report.md`；兼容清单：`docs/compatibility.md`。
- 来源与当前文件哈希：`docs/baseline.json`。
- 原始源码目录保持不变；本目录没有复制个人数据、真实配置和 Git 历史。

## 环境与复现

已验证 Python 3.11.16、macOS arm64。使用 uv 创建独立环境并同步锁定版本：

```sh
uv venv --python 3.11 .venv
uv pip sync --python .venv/bin/python requirements-dev.lock
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q agents forge tests scripts alembic
.venv/bin/python scripts/verify_baseline.py
.venv/bin/python scripts/verify_eval_freeze.py
uv pip check --python .venv/bin/python
```

`requirements-dev.lock` 锁定本次通过测试的直接和间接依赖版本；未锁定下载制品哈希，也未验证其他操作系统。`requirements.txt` 保留上游最低版本约束并新增 PyYAML，复现时使用 lock。

测试以合成数据、临时 HOME/cwd 和模拟模型响应执行，禁止 socket 连接。不要用真实 HOME 启动原 CLI 进行底座验证，它仍会自动发现个人 Memory、会话、MCP 与 Skill 配置。

## 进入下一阶段

阶段 5 的门禁测试已通过。阶段 6 的本机 API 和演示已通过。C 套作业会在计价后的 15 美元处停下，并且不晋升；直接执行 `scripts/run_live_eval.py --confirm-live` 才连接模型。`agents/` 里的旧循环仍只供阶段 1 回归使用。
