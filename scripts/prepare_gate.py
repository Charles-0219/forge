"""Approve frozen S1 and record a real runtime-suite pass. This script does not call a model."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forge.gate import approve_frozen_skill, runtime_digest, write_record
from forge.kernel import Kernel
from forge.sqlite_store import SqliteEventStore


def main() -> int:
    junit = ROOT / "artifacts" / "local" / "a-suite.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        print("运行时套件没有全部通过。这次不记通过数，也不批准。")
        return 1
    passed, total = _junit_counts(junit)
    if passed != total or total <= 0:
        print(f"运行时套件结果是 {passed}/{total}。这次不批准。")
        return 1
    digest = runtime_digest(ROOT)
    store = SqliteEventStore(ROOT / "artifacts" / "local" / "skill-gate.sqlite")
    approval = asyncio.run(_approve(store))
    record = write_record(
        ROOT / "evals" / "gate-record.json",
        approval,
        passed=passed,
        total=total,
        digest=digest,
    )
    print(
        f"approved {record['name']} {record['content_hash'][:12]} "
        f"status={record['status']} active={record['active']} a={passed}/{total}"
    )
    print("没有连接模型。")
    return 0


async def _approve(store: SqliteEventStore) -> dict:
    await store.open()
    try:
        return await approve_frozen_skill(Kernel(store), ROOT / "evals" / "suite.json")
    finally:
        await store.close()


def _junit_counts(path: Path) -> tuple[int, int]:
    root = ET.parse(path).getroot()
    suites = list(root) if root.tag == "testsuites" else [root]
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += int(suite.attrib.get("tests", 0))
        failures += int(suite.attrib.get("failures", 0))
        errors += int(suite.attrib.get("errors", 0))
        skipped += int(suite.attrib.get("skipped", 0))
    return tests - failures - errors - skipped, tests - skipped


if __name__ == "__main__":
    raise SystemExit(main())
