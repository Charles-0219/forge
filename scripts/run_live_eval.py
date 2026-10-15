"""Manual C-suite entry. Tests and imports do not connect to a model."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forge.c_suite import run_comparison, scores_from_report
from forge.coordinator import Coordinator
from forge.gate import runtime_counts, runtime_digest
from forge.hashing import sha256_text
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from forge.skills import live_guard


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None, *, poster=None, connect: bool = False) -> int:
    args = list(sys.argv if argv is None else argv)
    env = dict(os.environ if environ is None else environ)
    reason = live_guard(args, env)
    if reason == "manual":
        print("C 套真实调用需要手动加上 --confirm-live。本次没有连接模型。")
        return 2
    if reason == "pricing":
        print("需要 FORGE_USD_PER_MILLION 才能执行 15 美元上限。本次没有连接模型。")
        return 2
    if reason == "credential":
        print("需要环境变量 APIKEY。本次没有连接模型。")
        return 2
    if poster is None and not connect:
        print("手动评测条件已满足。本次没有连接模型。")
        return 0
    try:
        rate = float(env["FORGE_USD_PER_MILLION"])
    except ValueError:
        print("FORGE_USD_PER_MILLION 必须是数字。本次没有连接模型。")
        return 2
    if rate <= 0:
        print("FORGE_USD_PER_MILLION 必须大于 0。本次没有连接模型。")
        return 2
    candidate = _optional_text(args, "--candidate-file")
    if candidate is not None and not _matches_approval(candidate):
        print("候选文件与已批准的冻结技能哈希不一致。本次没有连接模型。")
        return 2
    if poster is None:
        poster = _deepseek_poster(env["APIKEY"])
    out = _output_dir(args)
    report = asyncio.run(_execute(poster, rate, out, args, candidate))
    print(
        f"complete={report['complete']} reason={report['reason']} "
        f"cost_usd={report['cost_usd']} promoted={report['promoted']}"
    )
    print(f"报告已写入 {out / 'report.json'}")
    return 0 if report["complete"] else 3


def _output_dir(args: list[str]) -> Path:
    if "--out" in args:
        found = Path(args[args.index("--out") + 1])
        found.mkdir(parents=True, exist_ok=True)
        return found
    return Path(tempfile.mkdtemp(prefix="forge-c-suite-"))


async def _execute(poster, rate: float, out: Path, args: list[str], candidate: str | None) -> dict:
    suite = json.loads((ROOT / "evals" / "suite.json").read_text(encoding="utf-8"))
    holdout = json.loads((ROOT / "evals" / "holdout.json").read_text(encoding="utf-8"))
    live = suite["live_eval"]
    kernel = Kernel(MemoryEventStore())
    report = await run_comparison(
        kernel,
        Coordinator(kernel),
        poster,
        dev_tasks=list(suite["dev_tasks"]),
        holdout_tasks=list(holdout["tasks"]),
        work_root=out / "work",
        usd_per_million=rate,
        cost_cap_usd=float(live["cost_cap_usd"]),
        model=str(live["model"]),
        candidate=candidate,
        active=_optional_text(args, "--active-file"),
    )
    passed, total = _recorded_runtime_counts()
    report["scores"] = scores_from_report(report, a_passed=passed, a_total=total)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _recorded_runtime_counts() -> tuple[int, int]:
    path = ROOT / "evals" / "gate-record.json"
    if not path.exists():
        return 0, 0
    record = json.loads(path.read_text(encoding="utf-8"))
    return runtime_counts(record, runtime_digest(ROOT))


def _matches_approval(body: str) -> bool:
    path = ROOT / "evals" / "gate-record.json"
    if not path.exists():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    return record.get("status") == "approved" and sha256_text(body) == record.get("content_hash")


def _optional_text(args: list[str], flag: str) -> str | None:
    if flag not in args:
        return None
    path = Path(args[args.index(flag) + 1])
    if path.name == "holdout.json":
        raise SystemExit("留出题不能作为候选或 active Skill。")
    return path.read_text(encoding="utf-8")


def _deepseek_poster(api_key: str):
    import httpx

    async def post(request: dict) -> dict:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=request,
            )
            response.raise_for_status()
            return response.json()

    return post


if __name__ == "__main__":
    raise SystemExit(main(connect=True))
