# Forge：第一阶段底座

本目录是 BearCode 运行时的干净导入与已验证缺陷修复版。当前仅完成“底座核验”，尚未实现 Forge 事件内核、统一权限、数据库 Skill 门禁和服务端。

许可证是 MIT。公开仓库是 <https://github.com/Charles-0219/forge>。权利与历史基线见 `PROVENANCE.md`。已批准的评测样本见 `evals/`。

阶段 2 增加了 `forge/` 事件内核。它记录会话、动作和双协议投影，并把同一套行为落到内存或 SQLite。

阶段 3 在同一内核上增加策略、审批、六个内置工具、恢复和折叠。执行入口是 `Coordinator`，不替换 `agents/` 里的原循环。结果见 `docs/phase3-report.md`。

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

阶段 3 的执行测试已通过。阶段 4 需要单独授权。在那之前，不把 Dispatcher 接进 `Agent.chat`，也不调用 DeepSeek。
