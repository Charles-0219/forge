from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from forge.coordinator import Coordinator
from forge.errors import Conflict, ForgeError
from forge.events import TERMINAL_ACTION_STATES
from forge.hashing import strip_secrets
from forge.isolation import read_memory
from forge.kernel import Kernel
from forge.skills import SkillService


def _keys_match(provided: str, expected: str) -> bool:
    left = hashlib.sha256(provided.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_ref: str = Field(min_length=1)
    protocol: str
    cwd: str = Field(min_length=1)
    mode: str = "default"
    confirm_strategy: str = "confirm_risky"
    config_version: str = "0"
    parent_id: str | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    client_request_id: str = Field(min_length=1)
    budget: dict[str, Any] | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    arguments_hash: str = Field(min_length=1)


class ModeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str


class SkillDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    content_hash: str
    decision: str
    suite_ref: str | None = None


class ExtractionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    content: str
    evidence: list[dict]
    source: str


class EvalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    suite_ref: str = Field(min_length=1)
    model_ref: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    config_version: str = "0"


class SimulatedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: dict[str, Any]


def create_app(
    kernel: Kernel,
    coordinator: Coordinator,
    skills: SkillService,
    *,
    api_key: str,
    poll_interval: float = 1.0,
    max_polls: int | None = None,
    allow_simulated_model: bool = False,
    manage_store: bool = False,
    console_path: Path | None = None,
) -> FastAPI:
    wakeups: dict[str, asyncio.Event] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_store:
            await kernel.store.open()
        yield
        if manage_store:
            await kernel.store.close()

    app = FastAPI(title="Forge", version="0.6.0", lifespan=lifespan)
    app.state.kernel = kernel
    app.state.coordinator = coordinator
    app.state.skills = skills
    app.state.api_key = api_key
    app.state.allow_simulated_model = allow_simulated_model

    @app.exception_handler(ForgeError)
    async def forge_error(_request: Request, exc: ForgeError):
        return Response(
            json.dumps({"detail": str(exc)}, ensure_ascii=False),
            status_code=exc.code,
            media_type="application/json",
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return Response(
            json.dumps({"detail": "invalid request"}, ensure_ascii=False),
            status_code=422,
            media_type="application/json",
        )

    def authorize(x_api_key: str | None = Header(default=None)) -> None:
        if x_api_key is None or not _keys_match(x_api_key, api_key):
            raise HTTPException(status_code=401, detail="unauthorized")

    def wake(conversation_id: str) -> None:
        wakeups.setdefault(conversation_id, asyncio.Event()).set()

    async def wait_for(conversation_id: str) -> None:
        event = wakeups.setdefault(conversation_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), poll_interval)
        except TimeoutError:
            return
        event.clear()

    @app.get("/")
    async def console():
        page = console_path or Path(__file__).resolve().parent / "static" / "console.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="console not found")
        return FileResponse(page)

    @app.post("/v1/conversations", status_code=201)
    async def create_conversation(payload: ConversationIn, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        conversation = await kernel.create_conversation(
            protocol=payload.protocol,
            model_ref=payload.model_ref,
            cwd=payload.cwd,
            mode=payload.mode,
            confirm_strategy=payload.confirm_strategy,
            config_version=payload.config_version,
            parent_id=payload.parent_id,
        )
        coordinator.configure(conversation.id, coordinator.configs.get(conversation.id) or _default_policy())
        return _public_conversation(conversation)

    @app.get("/v1/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        conversation = await kernel.get_conversation(conversation_id)
        active = None
        async with kernel.store.transaction() as unit:
            run = await unit.get_active_run(conversation_id)
            if run is not None:
                active = run.status
        body = _public_conversation(conversation)
        body["active_run_status"] = active
        return body

    @app.post("/v1/conversations/{conversation_id}/messages", status_code=201)
    async def submit_message(conversation_id: str, payload: MessageIn, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        run = await kernel.submit_message(
            conversation_id,
            body=payload.body,
            client_request_id=payload.client_request_id,
            budget=strip_secrets(payload.budget) if payload.budget is not None else None,
        )
        wake(conversation_id)
        return await _public_run(kernel, run)

    @app.get("/v1/conversations/{conversation_id}/events")
    async def list_events(
        conversation_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        x_api_key: str | None = Header(default=None),
    ):
        authorize(x_api_key)
        await kernel.get_conversation(conversation_id)
        selected = await _events_after(kernel, conversation_id, after_seq, limit)
        next_seq = selected[-1]["seq"] if selected else after_seq
        return {"events": selected, "next_seq": next_seq}

    @app.get("/v1/conversations/{conversation_id}/events/stream")
    async def stream_events(
        conversation_id: str,
        request: Request,
        after_seq: int | None = Query(default=None, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        x_api_key: str | None = Header(default=None),
    ):
        authorize(x_api_key)
        await kernel.get_conversation(conversation_id)
        cursor = after_seq if after_seq is not None else _header_seq(last_event_id)

        async def generate():
            current = cursor
            waits = 0
            while True:
                if await request.is_disconnected():
                    return
                batch = await _events_after(kernel, conversation_id, current, 500)
                for event in batch:
                    current = event["seq"]
                    yield _sse(event)
                if max_polls is not None and waits >= max_polls:
                    return
                await wait_for(conversation_id)
                waits += 1

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        return await _public_run(kernel, await kernel.get_run(run_id))

    @app.post("/v1/runs/{run_id}/pause")
    async def pause_run(run_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        return await _control(kernel, coordinator, run_id, "pause")

    @app.post("/v1/runs/{run_id}/resume")
    async def resume_run(run_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        return await _control(kernel, coordinator, run_id, "resume")

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        return await _control(kernel, coordinator, run_id, "cancel")

    @app.post("/v1/runs/{run_id}/approvals/{approval_id}")
    async def decide_approval(
        run_id: str,
        approval_id: str,
        payload: DecisionIn,
        x_api_key: str | None = Header(default=None),
    ):
        authorize(x_api_key)
        run = await kernel.get_run(run_id)
        async with kernel.store.transaction() as unit:
            approval = await unit.get_approval(approval_id)
        if approval is None or approval.conversation_id != run.conversation_id:
            raise HTTPException(status_code=404, detail="approval not found")
        if payload.decision not in {"approve", "reject"}:
            raise HTTPException(status_code=422, detail="decision must be approve or reject")
        saved = await coordinator.decide(approval_id, payload.decision, payload.arguments_hash)
        wake(run.conversation_id)
        return {
            "id": saved.id,
            "decision": saved.decision,
            "status": saved.status,
            "arguments_hash": saved.arguments_hash,
        }

    @app.post("/v1/conversations/{conversation_id}/compact")
    async def compact(conversation_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        event = await coordinator.fold(conversation_id)
        wake(conversation_id)
        if event is None:
            return {"folded": False, "event": None}
        return {"folded": True, "event": _public_event(event)}

    @app.post("/v1/conversations/{conversation_id}/mode")
    async def change_mode(conversation_id: str, payload: ModeIn, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        if payload.mode != "plan":
            raise HTTPException(status_code=409, detail="leave plan mode through plan approval")
        async with kernel.store.transaction() as unit:
            conversation = await kernel._require_conversation(unit, conversation_id)
            if await unit.get_active_run(conversation_id) is not None:
                raise HTTPException(status_code=409, detail="plan mode requires an idle conversation")
            if conversation.mode != "plan":
                previous = conversation.mode
                conversation = await unit.update_conversation(conversation_id, mode="plan")
                await kernel._append(unit, conversation_id, None, "mode_changed", {"mode": "plan", "previous": previous})
        wake(conversation_id)
        return _public_conversation(conversation)

    @app.get("/v1/skills")
    async def list_skills(
        scope: str | None = None,
        status: str | None = None,
        x_api_key: str | None = Header(default=None),
    ):
        authorize(x_api_key)
        async with kernel.store.transaction() as unit:
            skills_found = await unit.list_skills()
            versions = await unit.list_skill_versions()
        rows = []
        for skill in skills_found:
            if scope is not None and skill.scope != scope:
                continue
            summary = []
            for version in versions:
                if version.skill_id != skill.id:
                    continue
                if status is not None and version.status != status:
                    continue
                summary.append({
                    "id": version.id,
                    "status": version.status,
                    "content_hash": version.content_hash,
                    "source": version.source,
                    "suite_ref": version.suite_ref,
                    "block_reason": version.block_reason,
                })
            rows.append({
                "id": skill.id,
                "name": skill.name,
                "scope": skill.scope,
                "active_version_id": skill.active_version_id,
                "versions": summary,
            })
        return {"skills": rows}

    @app.get("/v1/skills/{skill_id}/versions/{version_id}")
    async def get_version(skill_id: str, version_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        async with kernel.store.transaction() as unit:
            version = await unit.get_skill_version(version_id)
            reports = await unit.list_eval_reports()
        if version is None or version.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="skill version not found")
        related = [
            {"id": report.id, "status": report.status, "promoted": report.promoted, "reason": report.reason}
            for report in reports
            if report.version_id == version.id
        ]
        return {
            "id": version.id,
            "skill_id": version.skill_id,
            "content": version.content,
            "content_hash": version.content_hash,
            "source": version.source,
            "evidence": strip_secrets(version.evidence),
            "parent_version_id": version.parent_version_id,
            "status": version.status,
            "suite_ref": version.suite_ref,
            "block_reason": version.block_reason,
            "reports": related,
        }

    @app.post("/v1/skills/{skill_id}/decision")
    async def decide_skill(skill_id: str, payload: SkillDecisionIn, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        async with kernel.store.transaction() as unit:
            version = await unit.get_skill_version(payload.version_id)
        if version is None or version.skill_id != skill_id:
            raise HTTPException(status_code=404, detail="skill version not found")
        if payload.decision == "approve":
            saved = await skills.approve(payload.version_id, content_hash=payload.content_hash, suite_ref=payload.suite_ref)
        elif payload.decision == "reject":
            saved = await skills.reject(payload.version_id)
        else:
            raise HTTPException(status_code=422, detail="decision must be approve or reject")
        return {"id": saved.id, "status": saved.status, "content_hash": saved.content_hash, "block_reason": saved.block_reason}

    @app.post("/v1/conversations/{conversation_id}/skill-extractions", status_code=201)
    async def extract_skill(conversation_id: str, payload: ExtractionIn, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        await kernel.get_conversation(conversation_id)
        version = await skills.propose(payload.name, payload.content, payload.evidence, source=payload.source)
        return {"id": version.id, "status": version.status, "content_hash": version.content_hash}

    @app.get("/v1/memories")
    async def list_memories(conversation_id: str = Query(min_length=1), x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        conversation = await kernel.get_conversation(conversation_id)
        return {"memories": read_memory(Path(conversation.cwd))}

    @app.post("/v1/evals", status_code=201)
    async def start_eval(payload: EvalIn, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        async with kernel.store.transaction() as unit:
            version = await unit.get_skill_version(payload.version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="skill version not found")
        if version.suite_ref != payload.suite_ref:
            raise Conflict("suite reference does not match the frozen approval")
        report = await skills.start_eval(
            payload.version_id,
            model_ref=payload.model_ref,
            parameters=strip_secrets(payload.parameters),
            config_version=payload.config_version,
        )
        return _public_report(report)

    @app.get("/v1/evals/latest")
    async def latest_eval(x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        async with kernel.store.transaction() as unit:
            reports = await unit.list_eval_reports()
        if not reports:
            raise HTTPException(status_code=404, detail="eval report not found")
        report = max(reports, key=lambda item: item.created_at)
        return _public_report(report)

    @app.get("/v1/evals/{report_id}")
    async def get_eval(report_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        async with kernel.store.transaction() as unit:
            report = await unit.get_eval_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="eval report not found")
        return _public_report(report)

    @app.get("/v1/attachments/{attachment_id}")
    async def get_attachment(attachment_id: str, x_api_key: str | None = Header(default=None)):
        authorize(x_api_key)
        record = coordinator.attachments.get(attachment_id)
        if record is None or "bytes" not in record:
            raise HTTPException(status_code=404, detail="attachment not found")
        return Response(content=record["bytes"], media_type="application/octet-stream")

    if allow_simulated_model:
        @app.post("/v1/conversations/{conversation_id}/runs/{run_id}/simulated-response")
        async def simulated_response(
            conversation_id: str,
            run_id: str,
            payload: SimulatedIn,
            x_api_key: str | None = Header(default=None),
        ):
            authorize(x_api_key)
            await kernel.record_response(conversation_id, run_id, strip_secrets(payload.response))
            await coordinator.dispatch(conversation_id, run_id)
            async with kernel.store.transaction() as unit:
                actions = [
                    action for action in await unit.list_actions(conversation_id)
                    if action.run_id == run_id
                ]
            if all(action.state in TERMINAL_ACTION_STATES for action in actions):
                await coordinator.finish(conversation_id, run_id)
            wake(conversation_id)
            return await _public_run(kernel, await kernel.get_run(run_id))

    return app


def _default_policy():
    from forge.policy import PolicyConfig
    return PolicyConfig()


def _header_seq(value: str | None) -> int:
    if value is None or not value.isdigit():
        return 0
    return int(value)


def _public_conversation(conversation) -> dict:
    return {
        "id": conversation.id,
        "protocol": conversation.protocol,
        "model_ref": conversation.model_ref,
        "cwd": conversation.cwd,
        "mode": conversation.mode,
        "confirm_strategy": conversation.confirm_strategy,
        "parent_id": conversation.parent_id,
    }


def _public_event(event) -> dict:
    return {
        "event_id": event.event_id,
        "seq": event.seq,
        "event_type": event.event_type,
        "run_id": event.run_id,
        "created_at": event.created_at,
        "payload": strip_secrets(event.payload),
    }


def _sse(event: dict) -> str:
    if event["event_type"] == "token_delta":
        return "event: token_delta\ndata: " + json.dumps(strip_secrets(event), ensure_ascii=False) + "\n\n"
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _events_after(kernel: Kernel, conversation_id: str, after_seq: int, limit: int) -> list[dict]:
    events = await kernel.list_events(conversation_id)
    selected = [_public_event(event) for event in events if event.seq > after_seq]
    return selected[:limit]


async def _public_run(kernel: Kernel, run) -> dict:
    async with kernel.store.transaction() as unit:
        approvals = await unit.list_approvals()
        actions = await unit.list_actions(run.conversation_id)
    action_ids = {action.action_id for action in actions if action.run_id == run.id}
    pending = []
    for approval in approvals:
        if approval.conversation_id != run.conversation_id or approval.status != "pending":
            continue
        if approval.subject_type == "action" and approval.subject_id not in action_ids:
            continue
        pending.append({
            "id": approval.id,
            "status": approval.status,
            "subject_type": approval.subject_type,
            "arguments_hash": approval.arguments_hash,
            "arguments": strip_secrets(approval.arguments),
        })
    return {
        "id": run.id,
        "conversation_id": run.conversation_id,
        "status": run.status,
        "client_request_id": run.client_request_id,
        "budget": strip_secrets(run.budget),
        "usage": strip_secrets(run.usage),
        "error": run.error,
        "pending_approvals": pending,
    }


async def _control(kernel: Kernel, coordinator: Coordinator, run_id: str, action: str) -> dict:
    run = await kernel.get_run(run_id)
    if action == "pause":
        updated = await coordinator.pause(run.conversation_id, run.id)
        control = "pause_requested" if run_id in coordinator.pause_requested else "paused"
    elif action == "resume":
        updated = await coordinator.resume(run.conversation_id, run.id)
        control = "resumed"
    else:
        updated = await coordinator.cancel(run.conversation_id, run.id)
        control = "cancelled"
    body = await _public_run(kernel, updated)
    body["control"] = control
    return body


def _public_report(report) -> dict:
    return {
        "id": report.id,
        "version_id": report.version_id,
        "status": report.status,
        "baseline": strip_secrets(report.baseline),
        "results": strip_secrets(report.results),
        "promoted": report.promoted,
        "reason": report.reason,
        "cost_usd": report.cost_usd,
        "created_at": report.created_at,
    }
