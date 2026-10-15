from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from forge.errors import Conflict, NotFound
from forge.events import new_id, utc_now
from forge.hashing import sha256_text, strip_secrets
from forge.kernel import Kernel
from forge.skill_records import EvalReport, Skill, SkillVersion


_FORBIDDEN_EVIDENCE = {"assistant_message", "observation", "retrieval", "tool_result"}
_CANDIDATE_SOURCES = {"add", "merge", "skill_create", "skill_evolve", "auto"}
def _tokens(text: str) -> set[str]:
    found = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", text):
        if re.fullmatch(r"[A-Za-z0-9_]+", chunk):
            found.add(chunk.lower())
            continue
        if len(chunk) == 1:
            found.add(chunk)
        for index in range(len(chunk) - 1):
            found.add(chunk[index:index + 2])
    return found


@dataclass(frozen=True)
class Promotion:
    promote: bool
    version_status: str
    report_status: str
    reason: str


def validate_evidence(evidence: list[dict]) -> None:
    if any(str(item.get("kind")) in _FORBIDDEN_EVIDENCE for item in evidence):
        raise Conflict("assistant output, tool results, and retrieval are not user evidence")
    if any(item.get("kind") == "preference" and item.get("explicit") for item in evidence):
        return
    feedback = {str(item.get("event_id")) for item in evidence if item.get("kind") == "user_feedback"}
    feedback.discard("None")
    if len(feedback) >= 2:
        return
    raise Conflict("a proposal needs two distinct user feedback events or one explicit preference")


def development_inputs(path: Path) -> dict:
    """Load the approved development suite. Holdout files stay out of this input."""
    if path.name == "holdout.json":
        raise Conflict("holdout tasks cannot enter candidate development")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "tasks" in data and "dev_tasks" not in data:
        raise Conflict("holdout tasks cannot enter candidate development")
    return {
        "skills": list(data.get("skills") or []),
        "retrieval": list(data.get("retrieval") or []),
        "dev_tasks": list(data.get("dev_tasks") or []),
    }


def retrieve(query: str, skills: list[SkillVersion], *, names: dict[str, str], k: int = 3) -> list[str]:
    wanted = _tokens(query)
    scored = []
    for version in skills:
        haystack = _tokens(f"{names.get(version.skill_id, '')} {version.content}")
        overlap = len(wanted & haystack)
        if overlap:
            scored.append((-overlap, version.id, names.get(version.skill_id, version.id)))
    scored.sort()
    return [item[1] for item in scored[:k]]


def retrieval_report(queries: list[dict], skills: list[tuple[str, str, str]]) -> dict:
    versions = [
        SkillVersion(
            id=skill_id,
            skill_id=skill_id,
            content=body,
            content_hash="",
            source="active",
            evidence=[],
            parent_version_id=None,
            status="active",
            suite_ref=None,
            block_reason=None,
            created_at="",
        )
        for skill_id, _name, body in skills
    ]
    names = {skill_id: name for skill_id, name, _body in skills}
    items = []
    recalls = []
    first_hits = []
    injections = 0
    for query in queries:
        got = retrieve(str(query["query"]), versions, names=names)
        relevant = [str(item) for item in query.get("relevant") or []]
        injections += sum(1 for item in got if item not in relevant)
        if relevant:
            recalls.append(len(set(got) & set(relevant)) / len(relevant))
            first_hits.append(1 if got and got[0] in relevant else 0)
        items.append({"id": query["id"], "retrieved": got, "relevant": relevant})
    return {
        "recall_at_3": (sum(recalls) / len(recalls)) if recalls else None,
        "first_hit_rate": (sum(first_hits) / len(first_hits)) if first_hits else None,
        "irrelevant_injections": injections,
        "items": items,
    }


def promotion_decision(
    *,
    baseline_changed: bool,
    cancelled: bool,
    complete: bool,
    call_failures: int,
    over_cap: bool,
    a_passed: int,
    a_total: int,
    hard_passed: int,
    hard_total: int,
    holdout_candidate: int,
    holdout_reference: int,
    new_permission_violations: int,
    new_secret_failures: int,
) -> Promotion:
    if baseline_changed:
        return Promotion(False, "approved", "completed", "baseline_changed")
    if cancelled:
        return Promotion(False, "approved", "cancelled", "cancelled")
    if over_cap or not complete or call_failures:
        return Promotion(False, "approved", "incomplete", "incomplete")
    if a_total <= 0 or a_passed != a_total:
        return Promotion(False, "eval_failed", "completed", "runtime_failed")
    if hard_passed != hard_total:
        return Promotion(False, "eval_failed", "completed", "hard_assertions")
    if new_permission_violations or new_secret_failures:
        return Promotion(False, "eval_failed", "completed", "safety")
    if holdout_candidate > holdout_reference:
        return Promotion(True, "active", "completed", "promoted")
    if holdout_candidate == holdout_reference:
        return Promotion(False, "eval_failed", "completed", "tie")
    return Promotion(False, "eval_failed", "completed", "not_better")


def build_live_request(task: dict, skill_body: str | None, *, model: str) -> dict:
    system = "Use the supplied skill when it applies. Do not write memory or start another evaluation."
    if skill_body:
        system += "\n\nSkill:\n" + skill_body
    user = str(task.get("prompt") or "")
    fixture = str(task.get("fixture") or "")
    if fixture:
        user = user + "\n" + fixture
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "thinking": {"type": "disabled"},
    }


def live_guard(argv: list[str], environ: dict[str, str]) -> str | None:
    """Return a refusal reason. None means the manual job may connect."""
    if "--confirm-live" not in argv:
        return "manual"
    if not environ.get("FORGE_USD_PER_MILLION"):
        return "pricing"
    if not environ.get("APIKEY"):
        return "credential"
    return None


async def compare_tasks(tasks, runner, *, candidate: str, active: str | None, work_root: Path) -> list[dict]:
    items = []
    for task in tasks:
        planned = [("none", None), ("candidate", candidate)]
        if active is None:
            items.append({"id": task["id"], "condition": "active", "applicable": False})
        else:
            planned.append(("active", active))
        for condition, body in planned:
            work = work_root / str(task["id"]) / condition
            work.mkdir(parents=True, exist_ok=True)
            context = {"memory_writes": False, "online_evolution": False, "condition": condition}
            outcome = await runner(task, body, work, context)
            items.append({"id": task["id"], "condition": condition, "applicable": True, "workdir": str(work), **outcome})
    return items


class SkillService:
    """Database skill gate. Only an active version is visible to search, inline, and fork."""

    def __init__(self, kernel: Kernel, *, scope: str) -> None:
        self.kernel = kernel
        self.scope = scope
        self._ledger_id: str | None = None

    async def propose(
        self,
        name: str,
        content: str,
        evidence: list[dict],
        *,
        source: str,
        parent_version_id: str | None = None,
    ) -> SkillVersion:
        if source == "baseline":
            raise Conflict("baseline import has its own command")
        if source not in _CANDIDATE_SOURCES:
            raise Conflict(f"unsupported candidate source: {source}")
        validate_evidence(evidence)
        await self._ledger()
        return await self._insert_version(
            name,
            content,
            evidence,
            source=source,
            status="proposed",
            parent_version_id=parent_version_id,
            event_type="skill_proposed",
        )

    async def import_candidate(self, name: str, content: str | None, *, path: str) -> SkillVersion | None:
        if content is None:
            return None
        await self._ledger()
        return await self._insert_version(
            name,
            content,
            [{"kind": "file", "path": path}],
            source="import",
            status="proposed",
            parent_version_id=None,
            event_type="skill_proposed",
        )

    async def import_baseline(self, name: str, content: str, *, authorization: str) -> SkillVersion:
        if not authorization.strip():
            raise Conflict("baseline import requires initialization authorization")
        await self._ledger()
        return await self._insert_version(
            name,
            content,
            [{"kind": "initialization", "authorization": authorization}],
            source="baseline",
            status="active",
            parent_version_id=None,
            event_type="skill_promoted",
        )

    async def approve(self, version_id: str, *, content_hash: str, suite_ref: str | None) -> SkillVersion:
        await self._ledger()
        async with self.kernel.store.transaction() as unit:
            version = await self._require_version(unit, version_id)
            if version.status != "proposed":
                raise Conflict("only a proposed version can be approved")
            if version.content_hash != content_hash:
                raise Conflict("content hash does not match the frozen proposal")
            version.status = "approved"
            version.suite_ref = suite_ref
            version.block_reason = None if suite_ref else "no_applicable_suite"
            saved = await unit.update_skill_version(version)
            await self._event(unit, "skill_decided", {
                "version_id": saved.id,
                "decision": "approve",
                "content_hash": saved.content_hash,
                "suite_ref": saved.suite_ref,
                "block_reason": saved.block_reason,
            })
            return saved

    async def reject(self, version_id: str) -> SkillVersion:
        await self._ledger()
        async with self.kernel.store.transaction() as unit:
            version = await self._require_version(unit, version_id)
            if version.status != "proposed":
                raise Conflict("only a proposed version can be rejected")
            version.status = "rejected"
            saved = await unit.update_skill_version(version)
            await self._event(unit, "skill_decided", {
                "version_id": saved.id,
                "decision": "reject",
                "content_hash": saved.content_hash,
            })
            return saved

    async def visible(self) -> list[dict]:
        async with self.kernel.store.transaction() as unit:
            rows = []
            for skill in await unit.list_skills():
                if skill.scope != self.scope or not skill.active_version_id:
                    continue
                version = await unit.get_skill_version(skill.active_version_id)
                if version is None or version.status != "active":
                    continue
                rows.append({
                    "name": skill.name,
                    "version_id": version.id,
                    "content_hash": version.content_hash,
                    "content": version.content,
                })
            return rows

    async def search(self, query: str) -> list[str]:
        visible = await self.visible()
        skills = [(item["name"], item["name"], item["content"]) for item in visible]
        found = set(retrieval_report(
            [{"id": "q", "query": query, "relevant": []}],
            skills,
        )["items"][0]["retrieved"])
        return [item["name"] for item in visible if item["name"] in found]

    async def start_eval(self, version_id: str, *, model_ref: str, parameters: dict, config_version: str) -> EvalReport:
        await self._ledger()
        async with self.kernel.store.transaction() as unit:
            if any(report.status == "running" for report in await unit.list_eval_reports()):
                raise Conflict("an evaluation occupies the execution slot")
            for conversation in await unit.list_conversations():
                if await unit.get_active_run(conversation.id) is not None:
                    raise Conflict("evaluation cannot start while a root task is running")
            version = await self._require_version(unit, version_id)
            if version.status != "approved":
                raise Conflict("evaluation requires an approved version")
            if version.block_reason:
                raise Conflict(version.block_reason)
            skill = await unit.get_skill(version.skill_id)
            active_hash = None
            if skill.active_version_id:
                active = await unit.get_skill_version(skill.active_version_id)
                active_hash = None if active is None else active.content_hash
            version.status = "evaluating"
            await unit.update_skill_version(version)
            report = EvalReport(
                id=new_id(),
                version_id=version.id,
                status="running",
                baseline=strip_secrets({
                    "active_version_id": skill.active_version_id,
                    "active_hash": active_hash,
                    "candidate_hash": version.content_hash,
                    "model_ref": model_ref,
                    "parameters": parameters,
                    "suite_ref": version.suite_ref,
                    "config_version": config_version,
                    "memory_writes": False,
                    "online_evolution": False,
                }),
                results=None,
                promoted=False,
                reason=None,
                cost_usd=None,
                created_at=utc_now(),
            )
            await unit.insert_eval_report(report)
            return report

    async def complete_eval(
        self,
        report_id: str,
        results: dict,
        *,
        pricing_usd_per_million: float | None = None,
        cost_cap_usd: float | None = None,
    ) -> EvalReport:
        await self._ledger()
        clean = strip_secrets(results)
        async with self.kernel.store.transaction() as unit:
            report = await unit.get_eval_report(report_id)
            if report is None:
                raise NotFound(f"eval report not found: {report_id}")
            if report.status != "running":
                raise Conflict("evaluation is not running")
            version = await self._require_version(unit, report.version_id)
            skill = await unit.get_skill(version.skill_id)
            tokens = int(clean.get("tokens") or 0)
            cost = None
            over_cap = False
            if pricing_usd_per_million is not None:
                cost = tokens / 1_000_000 * pricing_usd_per_million
                over_cap = cost_cap_usd is not None and cost > cost_cap_usd
            reference = clean.get("holdout_active")
            if report.baseline.get("active_version_id") is None:
                reference = clean.get("holdout_none")
            decision = promotion_decision(
                baseline_changed=skill.active_version_id != report.baseline.get("active_version_id"),
                cancelled=bool(clean.get("cancelled")),
                complete=bool(clean.get("complete")),
                call_failures=int(clean.get("call_failures") or 0),
                over_cap=over_cap,
                a_passed=int((clean.get("a") or {}).get("passed") or 0),
                a_total=int((clean.get("a") or {}).get("total") or 0),
                hard_passed=int(clean.get("hard_passed") or 0),
                hard_total=int(clean.get("hard_total") or 0),
                holdout_candidate=int(clean.get("holdout_candidate") or 0),
                holdout_reference=int(reference or 0),
                new_permission_violations=int(clean.get("new_permission_violations") or 0),
                new_secret_failures=int(clean.get("new_secret_failures") or 0),
            )
            if decision.promote:
                if skill.active_version_id:
                    previous = await unit.get_skill_version(skill.active_version_id)
                    if previous is not None:
                        previous.status = "superseded"
                        await unit.update_skill_version(previous)
                version.status = "active"
                await unit.update_skill_version(version)
                skill.active_version_id = version.id
                await unit.update_skill(skill)
                await self._event(unit, "skill_promoted", {
                    "version_id": version.id,
                    "content_hash": version.content_hash,
                    "report_id": report.id,
                })
            else:
                version.status = decision.version_status
                await unit.update_skill_version(version)
            report.status = decision.report_status
            report.results = clean
            report.promoted = decision.promote
            report.reason = decision.reason
            report.cost_usd = None if cost is None else f"{cost:.6f}"
            saved = await unit.update_eval_report(report)
            await self._event(unit, "skill_eval_completed", {
                "version_id": version.id,
                "report_id": report.id,
                "promoted": decision.promote,
                "reason": decision.reason,
            })
            return saved

    async def export_active(self, name: str, dest: Path, *, replace=os.replace) -> Path:
        await self._ledger()
        async with self.kernel.store.transaction() as unit:
            skill = await unit.get_skill_by_name(self.scope, name)
            if skill is None or not skill.active_version_id:
                raise NotFound(f"active skill not found: {name}")
            version = await unit.get_skill_version(skill.active_version_id)
            content = version.content
            active_id = skill.active_version_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_name(dest.name + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            replace(temporary, dest)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            await self._record_export_error(name, str(exc))
            current = await self._active_id(name)
            if current != active_id:
                raise Conflict("export failure changed the active skill")
            raise
        return dest

    async def _record_export_error(self, name: str, message: str) -> None:
        async with self.kernel.store.transaction() as unit:
            skill = await unit.get_skill_by_name(self.scope, name)
            skill.export_error = message
            await unit.update_skill(skill)
            await self._event(unit, "skill_eval_completed", {
                "name": name,
                "export_error": message,
                "active_version_id": skill.active_version_id,
            })

    async def _active_id(self, name: str) -> str | None:
        async with self.kernel.store.transaction() as unit:
            skill = await unit.get_skill_by_name(self.scope, name)
            return None if skill is None else skill.active_version_id

    async def _insert_version(
        self,
        name: str,
        content: str,
        evidence: list,
        *,
        source: str,
        status: str,
        parent_version_id: str | None,
        event_type: str,
    ) -> SkillVersion:
        async with self.kernel.store.transaction() as unit:
            skill = await unit.get_skill_by_name(self.scope, name)
            if skill is None:
                skill = Skill(
                    id=new_id(),
                    name=name,
                    scope=self.scope,
                    active_version_id=None,
                    export_error=None,
                    created_at=utc_now(),
                )
                await unit.insert_skill(skill)
            elif status == "active" and skill.active_version_id:
                raise Conflict("baseline import cannot replace an active skill")
            version = SkillVersion(
                id=new_id(),
                skill_id=skill.id,
                content=content,
                content_hash=sha256_text(content),
                source=source,
                evidence=list(evidence),
                parent_version_id=parent_version_id,
                status=status,
                suite_ref=None,
                block_reason=None,
                created_at=utc_now(),
            )
            await unit.insert_skill_version(version)
            if status == "active":
                skill.active_version_id = version.id
                await unit.update_skill(skill)
            await self._event(unit, event_type, {
                "version_id": version.id,
                "skill_id": skill.id,
                "name": name,
                "content_hash": version.content_hash,
                "source": source,
                "status": status,
            })
            return version

    async def _require_version(self, unit, version_id: str) -> SkillVersion:
        version = await unit.get_skill_version(version_id)
        if version is None:
            raise NotFound(f"skill version not found: {version_id}")
        return version

    async def _ledger(self) -> str:
        if self._ledger_id is not None:
            return self._ledger_id
        conversation = await self.kernel.create_conversation(
            protocol="openai",
            model_ref="skill-ledger",
            cwd=self.scope,
            mode="default",
            confirm_strategy="never_confirm",
        )
        self._ledger_id = conversation.id
        return conversation.id

    async def _event(self, unit, event_type: str, payload: dict):
        if self._ledger_id is None:
            raise Conflict("skill ledger is not open")
        return await self.kernel._append(unit, self._ledger_id, None, event_type, strip_secrets(payload))
