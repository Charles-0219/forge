from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Skill:
    id: str
    name: str
    scope: str
    active_version_id: str | None
    export_error: str | None
    created_at: str


@dataclass
class SkillVersion:
    id: str
    skill_id: str
    content: str
    content_hash: str
    source: str
    evidence: list
    parent_version_id: str | None
    status: str
    suite_ref: str | None
    block_reason: str | None
    created_at: str


@dataclass
class EvalReport:
    id: str
    version_id: str
    status: str
    baseline: dict
    results: dict | None
    promoted: bool
    reason: str | None
    cost_usd: str | None
    created_at: str
