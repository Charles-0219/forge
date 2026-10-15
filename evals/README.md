# 已冻结评测样本

2026-10-15 所有者批准了全部草案，批准原文是「是的」。

| 文件 | 内容 |
|---|---|
| `suite.json` | S1–S8、Q1–Q20、D1–D12，以及 C 套调用约定 |
| `holdout.json` | H1–H8 |
| `checksums.json` | 上述两个文件的 SHA-256 |

C 套约定：模型 `deepseek-flash`，OpenAI 兼容地址 `https://api.deepseek.com`，关闭思考，一次完整运行费用硬上限 15 美元。真实调用只从 `scripts/run_live_eval.py --confirm-live` 进入，并且要同时提供 `APIKEY` 和 `FORGE_USD_PER_MILLION`。回归测试不会走到这一步。

`holdout.json` 随公开仓库可见。晋升时仍然禁止把留出题写进 Skill 候选和开发提示。对外不能声称这些题目对读过仓库的人不可见。

修改样本必须先得到新的逐条批准，然后更新文件和 `checksums.json`。校验命令：

```sh
python3 scripts/verify_eval_freeze.py
```
