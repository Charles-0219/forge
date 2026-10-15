# Forge 第五阶段：Skill 门禁报告

## 结论与范围

阶段 5 门禁通过。Skill 的候选、批准、评测和晋升都在数据库里完成。全量测试 **174 passed**。回归没有调用 DeepSeek，也没有读取留出题的正文来编写 Skill。

`Coordinator` 和 Skill 门禁都还没有接进 `Agent.chat`。

## 交付

- `forge/skills.py`：候选只接受两条不同的用户反馈，或一条明确的长期偏好。助手输出、工具结果和检索内容不能当作用户证据。批准后内容哈希不可改。没有适用套件时停在 approved，并记下阻塞原因。检索、inline 和 fork 只看见 active 版本。
- 外部 `SKILL.md` 变化只生成导入候选。文件删除不移除 active。初始化基线走单独的导入命令。
- 导出先写临时文件再替换。替换失败时 active 指针不变，错误单独记录。
- 评测占用同一个执行槽。有未结束的根任务时不能开评；评测进行中不能再开新的根任务。
- 晋升在一个事务里核对基线。留出通过数必须严格更高。平分、运行时失败、安全失败、费用超限、取消和不完整都不晋升。评测期间 active 变了，报告留下，但不晋升。
- A 套沿用阶段 1–4 回归加上本阶段测试。B 套对冻结的 20 条查询给出 Recall@3、首位命中率、无关注入数和逐条结果。C 套的对照函数按无 Skill、当前 active、候选分目录运行，并关闭 Memory 写回。
- `scripts/run_live_eval.py`：没有 `--confirm-live`、`APIKEY` 和 `FORGE_USD_PER_MILLION` 时直接退出，不连接模型。请求体使用 `deepseek-flash`，并把 thinking 设为 disabled。

## 已验证的行为

| 检查 | 结果 |
|---|---|
| 未批准和未晋升的版本不会被检索到 | 通过 |
| 删除外部文件不删除 active；导出失败不改变 active | 通过 |
| 留出文件不能进入开发输入 | 通过 |
| 评测上下文关闭 Memory 写回，条件之间目录分开 | 通过 |
| 密钥字段不会进入评测结果和事件 | 通过 |
| 平分、运行时失败、基线变化、超过 15 美元都不晋升 | 通过 |
| 评测槽与根任务互斥 | 通过 |
| 阶段 1–4 与评测冻结 | 通过 |
| 全量测试 | 174 passed |

命令：

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q agents forge tests scripts alembic
.venv/bin/python scripts/verify_baseline.py
.venv/bin/python scripts/verify_eval_freeze.py
.venv/bin/python -m pip check
```

环境仍是 Python 3.11.16、macOS arm64。测试使用合成结果、临时目录和冻结的开发套件。

## 仍未解决

- 旧的 `agents/skills.py` 仍会直接写 Skill 文件。新门禁还没有接进 `Agent.chat`。
- 真实 C 套没有启动。手动命令是 `scripts/run_live_eval.py --confirm-live`，同时需要 `APIKEY` 和 `FORGE_USD_PER_MILLION`。没有单价时不会为了估算费用去连接模型。
- B 套现在是字词重叠检索，不是向量检索。报告只记录命中数，不把小样本写成稳定的效果结论。
- 原仓库 `data/` 里的公开榜不进入这三套评测。
