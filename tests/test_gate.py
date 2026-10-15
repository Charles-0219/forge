from __future__ import annotations

import json
from pathlib import Path

from forge.gate import approve_frozen_skill, runtime_counts, write_record
from forge.hashing import sha256_text
from forge.kernel import Kernel
from forge.sqlite_store import SqliteEventStore


ROOT = Path(__file__).resolve().parents[1]


async def test_frozen_s1_is_approved_without_becoming_active(tmp_path):
    store = SqliteEventStore(tmp_path / "gate.sqlite")
    await store.open()
    try:
        kernel = Kernel(store)
        first = await approve_frozen_skill(kernel, ROOT / "evals" / "suite.json")
        second = await approve_frozen_skill(kernel, ROOT / "evals" / "suite.json")
    finally:
        await store.close()
    suite = json.loads((ROOT / "evals" / "suite.json").read_text(encoding="utf-8"))
    body = next(item["body"] for item in suite["skills"] if item["id"] == "S1")
    assert first["version_id"] == second["version_id"]
    assert first["status"] == "approved"
    assert first["active"] is False
    assert first["content_hash"] == sha256_text(body)
    assert first["suite_ref"] == "evals/suite.json"


def test_runtime_counts_follow_the_tree_digest(tmp_path):
    record = write_record(
        tmp_path / "gate-record.json",
        {
            "skill_id": "S1",
            "name": "read-before-edit",
            "version_id": "v1",
            "content_hash": "abc",
            "status": "approved",
            "active": False,
            "suite_ref": "evals/suite.json",
        },
        passed=4,
        total=4,
        digest="tree-a",
    )
    assert runtime_counts(record, "tree-a") == (4, 4)
    assert runtime_counts(record, "tree-b") == (0, 0)
    record["a_passed"] = 3
    assert runtime_counts(record, "tree-a") == (0, 0)
