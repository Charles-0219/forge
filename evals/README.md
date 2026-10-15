# 已冻结评测样本

2026-10-15 所有者批准了全部草案，批准原文是「是的」。

| 文件 | 内容 |
|---|---|
| `suite.json` | S1–S8、Q1–Q20、D1–D12，以及 C 套调用约定 |
| `holdout.json` | H1–H8 |
| `checksums.json` | 上述两个文件的 SHA-256 |

C 套约定：模型 `deepseek-flash`，OpenAI 兼容地址 `https://api.deepseek.com`，关闭思考，一次完整运行费用硬上限 15 美元。直接执行 `scripts/run_live_eval.py --confirm-live` 才会连接模型，并且要同时提供 `APIKEY` 和 `FORGE_USD_PER_MILLION`。作业会跑 12 条开发题和 8 条留出题的无 Skill 对照；`--candidate-file` 和 `--active-file` 才加入另外两路。没有 active 文件时，这一路记为不适用，不调用模型。单价会用在全部 token 上，建议取官方峰值输出价。测试调用 `main()` 时不连接。费用达到上限、调用失败、断言无法计分，或本地 A 套结果没有另行记入时，都不晋升。留出文件不能当作候选。

`holdout.json` 随公开仓库可见。晋升时仍然禁止把留出题写进 Skill 候选和开发提示。对外不能声称这些题目对读过仓库的人不可见。

修改样本必须先得到新的逐条批准，然后更新文件和 `checksums.json`。校验命令：

```sh
python3 scripts/verify_eval_freeze.py
```
