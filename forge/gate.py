"""Local promotion gate. This module never calls a model."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from forge.errors import Conflict
from forge.hashing import sha256_text
from forge.kernel import Kernel
from forge.skills import SkillService, development_inputs


SCOPE = "forge"
SUITE_REF = "evals/suite.json"
RECORD_NAME = "gate-record.json"
_FOLDERS = ("agents", "forge", "scripts", "tests")


def frozen_skill(suite_path: Path, skill_id: str = "S1") -> dict:
    """Return one approved development skill. Holdout files are refused before reading."""
    suite = development_inputs(suite_path)
    matches = [item for item in suite["skills"] if item.get("id") == skill_id]
    if len(matches) != 1:
        raise Conflict(f"development suite does not contain exactly one {skill_id}")
    return matches[0]


def runtime_digest(root: Path) -> str:
    """Hash the runtime sources. The gate record itself is outside this digest."""
    digest = hashlib.sha256()
    for folder in _FOLDERS:
        base = root / folder
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def runtime_counts(record: dict, digest: str) -> tuple[int, int]:
    """Use a recorded pass only when it matches this tree and every test passed."""
    passed = int(record.get("a_passed") or 0)
    total = int(record.get("a_total") or 0)
    if record.get("runtime_digest") != digest or passed != total or total <= 0:
        return 0, 0
    return passed, total


async def approve_frozen_skill(kernel: Kernel, suite_path: Path, *, skill_id: str = "S1") -> dict:
    """Propose and approve the frozen body. The version stays approved, not active."""
    skill = frozen_skill(suite_path, skill_id)
    body = str(skill["body"])
    content_hash = sha256_text(body)
    service = SkillService(kernel, scope=SCOPE)
    existing = await _matching_version(kernel, str(skill["name"]), content_hash)
    if existing is None:
        proposed = await service.propose(
            str(skill["name"]),
            body,
            [{
                "kind": "preference",
                "explicit": True,
                "text": "所有者于 2026-10-15 选定 S1 read-before-edit，正文使用冻结原句。",
            }],
            source="add",
        )
        existing = await service.approve(proposed.id, content_hash=content_hash, suite_ref=SUITE_REF)
    async with kernel.store.transaction() as unit:
        stored = await unit.get_skill_by_name(SCOPE, str(skill["name"]))
        version = await unit.get_skill_version(existing.id)
    if stored.active_version_id is not None:
        raise Conflict("approval changed the active skill")
    if version.status != "approved" or version.content != body:
        raise Conflict("approved content does not match the frozen skill")
    return {
        "skill_id": skill_id,
        "name": skill["name"],
        "version_id": version.id,
        "content_hash": version.content_hash,
        "status": version.status,
        "active": False,
        "suite_ref": version.suite_ref,
    }


async def _matching_version(kernel: Kernel, name: str, content_hash: str):
    async with kernel.store.transaction() as unit:
        skill = await unit.get_skill_by_name(SCOPE, name)
        if skill is None:
            return None
        if skill.active_version_id is not None:
            raise Conflict("frozen skill is already active")
        for version in await unit.list_skill_versions(skill.id):
            if version.content_hash == content_hash and version.status == "approved" and version.suite_ref == SUITE_REF:
                return version
        return None


def write_record(path: Path, approval: dict, *, passed: int, total: int, digest: str) -> dict:
    record = {
        "schema_version": 1,
        **approval,
        "usd_per_million": "1.20",
        "pricing_basis": "所有者于 2026-10-15 选定官方峰值输出价。调用时仍须设置 FORGE_USD_PER_MILLION。",
        "a_passed": passed,
        "a_total": total,
        "runtime_digest": digest,
        "recorded_on": "2026-10-15",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record
