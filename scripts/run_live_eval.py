"""Manual C-suite entry. Tests and imports do not connect to a model."""

from __future__ import annotations

import os
import sys

from forge.skills import live_guard


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    reason = live_guard(list(sys.argv if argv is None else argv), dict(os.environ if environ is None else environ))
    if reason == "manual":
        print("C 套真实调用需要手动加上 --confirm-live。本次没有连接模型。")
        return 2
    if reason == "pricing":
        print("需要 FORGE_USD_PER_MILLION 才能执行 15 美元上限。本次没有连接模型。")
        return 2
    if reason == "credential":
        print("需要环境变量 APIKEY。本次没有连接模型。")
        return 2
    print("手动评测条件已满足。请在单独授权的作业里启动，不要把它放进回归测试。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
