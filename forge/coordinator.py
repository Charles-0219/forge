from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path

from forge.dispatcher import Dispatcher, Job, ToolResult
from forge.errors import Conflict, Gone, NotFound
from forge.events import TERMINAL_ACTION_STATES, TERMINAL_RUN_STATES, Action, Approval, Event, Run, new_id, utc_now
from forge.fold import build_summary, fold_targets
from forge.hashing import arguments_hash
from forge.kernel import Kernel
from forge.paths import PathEscape, file_digest, resolve_inside
from forge.policy import PolicyConfig, evaluate


_ALLOWED = {
    "queued": {"running", "paused", "cancelled", "failed", "interrupted", "succeeded"},
    "running": {"waiting_approval", "paused", "succeeded", "failed", "cancelled", "interrupted"},
    "waiting_approval": {"running", "paused", "succeeded", "failed", "cancelled", "interrupted"},
    "paused": {"running", "waiting_approval", "succeeded", "failed", "cancelled", "interrupted"},
}
_UNKNOWN = "Result unknown: the tool outcome could not be confirmed."


@dataclass
class _Step:
    kind: str
    job: Job | None = None


@dataclass
class _Recorded:
    job: Job | None = None
    resume: bool = False
    conversation_id: str = ""
    run_id: str = ""


class Coordinator:
    """Phase-3 execution path. Call recover() before dispatch when reopening a store."""

    def __init__(self, kernel: Kernel, *, attachment_dir: Path | None = None, output_limit: int = 8192) -> None:
        self.kernel = kernel
        self.configs: dict[str, PolicyConfig] = {}
        self.pause_requested: set[str] = set()
        self.cancel_requested: set[str] = set()
        self.processes: dict = {}
        self.attachments: dict[str, dict] = {}
        self.after_execute = None
        self.mcp = None
        self.dispatcher = Dispatcher(
            processes=self.processes,
            attachments=self.attachments,
            attachment_dir=attachment_dir,
            output_limit=output_limit,
        )

    def configure(self, conversation_id: str, config: PolicyConfig) -> None:
        self.configs[conversation_id] = config

    def bind_mcp(self, registry) -> None:
        self.mcp = registry

    async def _run_job(self, job):
        if job.tool_name.startswith("mcp__"):
            if self.mcp is None:
                from forge.dispatcher import ToolResult
                return ToolResult("error", "", error={"code": "mcp_unavailable"})
            return await self.mcp.call(job)
        return await self.dispatcher.run(job)

    async def dispatch(self, conversation_id: str, run_id: str) -> None:
        for _ in range(10000):
            step = await self._claim(conversation_id, run_id)
            if step.kind == "stop":
                return
            if step.kind == "again":
                continue
            result = await self._run_job(step.job)
            if self.after_execute is not None:
                self.after_execute()
            await self._commit_result(step.job, result)
        raise Conflict("dispatch did not advance")

    async def decide(self, approval_id: str, decision: str, arguments_hash_value: str) -> Approval:
        if decision not in {"approve", "reject"}:
            raise Conflict(f"unsupported approval decision: {decision}")
        recorded = await self._record_decision(approval_id, decision, arguments_hash_value)
        if recorded.job is not None:
            result = await self._run_job(recorded.job)
            if self.after_execute is not None:
                self.after_execute()
            await self._commit_result(recorded.job, result)
            await self.dispatch(recorded.job.conversation_id, recorded.job.run_id)
        elif recorded.resume:
            await self.dispatch(recorded.conversation_id, recorded.run_id)
        return await self._approval(approval_id)

    async def pause(self, conversation_id: str, run_id: str) -> Run:
        async with self.kernel.store.transaction() as unit:
            run = await self._run(unit, conversation_id, run_id)
            actions = await unit.list_actions(conversation_id)
            if any(action.state == "executing" and action.run_id == run_id for action in actions):
                self.pause_requested.add(run_id)
                return run
            self.pause_requested.discard(run_id)
            return await self._set_status(unit, run, "paused")

    async def resume(self, conversation_id: str, run_id: str) -> Run:
        self.pause_requested.discard(run_id)
        async with self.kernel.store.transaction() as unit:
            run = await self._run(unit, conversation_id, run_id)
            if run.status in TERMINAL_RUN_STATES:
                raise Conflict("terminal run cannot resume")
            if run.status != "paused":
                raise Conflict("run is not paused")
            actions = await unit.list_actions(conversation_id)
            waiting = any(action.state == "waiting_approval" and action.run_id == run_id for action in actions)
            return await self._set_status(unit, run, "waiting_approval" if waiting else "running")

    async def cancel(self, conversation_id: str, run_id: str) -> Run:
        self.cancel_requested.add(run_id)
        proc = self.processes.get(run_id)
        if proc is not None:
            from forge.dispatcher import _kill_group
            _kill_group(proc)
        async with self.kernel.store.transaction() as unit:
            run = await self._run(unit, conversation_id, run_id)
            actions = _ordered(await unit.list_events(conversation_id), await unit.list_actions(conversation_id), run_id)
            await self._reject_unstarted(unit, actions, "cancelled")
            if run.status != "cancelled":
                run = await self._set_status(unit, run, "cancelled")
            return run

    async def finish(self, conversation_id: str, run_id: str, status: str = "succeeded") -> Run:
        async with self.kernel.store.transaction() as unit:
            run = await self._run(unit, conversation_id, run_id)
            return await self._set_status(unit, run, status)

    async def recover(self) -> dict:
        async with self.kernel.store.transaction() as unit:
            changes = 0
            for approval in await unit.list_approvals():
                if approval.status != "pending":
                    continue
                approval.status = "expired"
                await unit.update_approval(approval)
                changes += 1
            for action in await unit.list_actions():
                if action.state in TERMINAL_ACTION_STATES:
                    continue
                if action.state == "executing":
                    await self._unknown(unit, action)
                else:
                    await self._reject(unit, action, "interrupted before execution")
                changes += 1
            for run in await unit.list_runs():
                if run.status in TERMINAL_RUN_STATES:
                    continue
                await self._set_status(unit, run, "interrupted")
                changes += 1
            return {"changes": changes}

    async def submit_plan(self, conversation_id: str, *, body: str, path: str) -> Approval:
        async with self.kernel.store.transaction() as unit:
            conversation = await self.kernel._require_conversation(unit, conversation_id)
            if conversation.mode != "plan":
                raise Conflict("plan approval requires plan mode")
            config = self.configs.get(conversation_id, PolicyConfig())
            canonical, digest = _plan_file(conversation.cwd, config.plan_path, path, body)
            payload = {"body": body, "path": canonical}
            approval = Approval(
                id=new_id(),
                conversation_id=conversation_id,
                subject_type="plan",
                subject_id=conversation_id,
                decision=None,
                decided_at=None,
                created_at=utc_now(),
                arguments_hash=arguments_hash(payload),
                arguments=payload,
                status="pending",
                basis={"canonical_path": canonical, "content_hash": digest},
            )
            return await unit.insert_approval(approval)

    async def fold(self, conversation_id: str) -> Event | None:
        async with self.kernel.store.transaction() as unit:
            if await unit.get_conversation(conversation_id) is None:
                raise NotFound(f"conversation not found: {conversation_id}")
            actions = await unit.list_actions(conversation_id)
            if any(action.state == "executing" for action in actions):
                raise Conflict("fold is not at a safe point")
            events = await unit.list_events(conversation_id)
            selected = fold_targets(events)
            if not selected:
                return None
            latest = next((event for event in reversed(events) if event.event_type == "condensation"), None)
            active = await unit.get_active_run(conversation_id)
            return await self.kernel._append(unit, conversation_id, active.id if active else None, "condensation", {
                "replaces": [event.event_id for event in selected],
                "summary": build_summary(selected),
                "generator": {"name": "phase3.fold", "version": "1"},
                "replaces_condensation_id": latest.event_id if latest else None,
            })

    async def _claim(self, conversation_id: str, run_id: str) -> _Step:
        async with self.kernel.store.transaction() as unit:
            conversation = await self.kernel._require_conversation(unit, conversation_id)
            run = await self._run(unit, conversation_id, run_id)
            events = await unit.list_events(conversation_id)
            actions = _ordered(events, await unit.list_actions(conversation_id), run_id)
            if run.status in TERMINAL_RUN_STATES:
                return _Step("stop")
            if run_id in self.cancel_requested:
                await self._reject_unstarted(unit, actions, "cancelled")
                if run.status != "cancelled":
                    await self._set_status(unit, run, "cancelled")
                self.cancel_requested.discard(run_id)
                return _Step("stop")
            if any(action.state == "executing" for action in actions):
                raise Conflict("action is executing and has no result; recover the run before dispatching")
            if run_id in self.pause_requested or run.status == "paused":
                if run.status != "paused":
                    await self._set_status(unit, run, "paused")
                self.pause_requested.discard(run_id)
                return _Step("stop")
            action = next((item for item in actions if item.state not in TERMINAL_ACTION_STATES), None)
            if action is None:
                return _Step("stop")
            if action.state == "waiting_approval":
                return _Step("stop")
            decision = evaluate(
                tool_name=action.tool_name,
                arguments=action.arguments,
                mode=conversation.mode,
                strategy=conversation.confirm_strategy,
                config=self.configs.get(conversation_id, PolicyConfig()),
                cwd=conversation.cwd,
                events=events,
                cancelled=False,
                budget_blocked=_budget_blocked(run, actions),
                confirmed=action.state == "approved",
            )
            if decision.outcome == "deny":
                await self._reject(unit, action, decision.reason)
                await self._sync_wait(unit, run)
                return _Step("again")
            if decision.outcome == "confirm":
                await self._open_approval(unit, conversation_id, action)
                fresh = await unit.get_run(run.id)
                await self._set_status(unit, fresh, "waiting_approval")
                return _Step("stop")
            await unit.update_action_state(action.action_id, state="executing")
            await self.kernel._append(unit, conversation_id, run_id, "action_started", {
                "action_id": action.action_id,
                "tool_call_id": action.tool_call_id,
                "tool_name": action.tool_name,
            })
            fresh = await unit.get_run(run.id)
            if fresh.status == "waiting_approval":
                await self._set_status(unit, fresh, "running")
            return _Step("run", _job(self, conversation.cwd, action))

    async def _record_decision(self, approval_id: str, decision: str, arguments_hash_value: str) -> _Recorded:
        async with self.kernel.store.transaction() as unit:
            approval = await unit.get_approval(approval_id)
            if approval is None:
                raise NotFound(f"approval not found: {approval_id}")
            if approval.status == "expired":
                raise Gone("approval expired")
            if approval.arguments_hash != arguments_hash_value:
                raise Conflict("approval arguments do not match")
            if approval.decision == decision:
                return _Recorded()
            if approval.decision is not None:
                raise Conflict("approval already has a different decision")
            if approval.subject_type == "plan":
                await self._decide_plan(unit, approval, decision)
                return _Recorded()
            action = await unit.get_action(approval.subject_id)
            if action is None or action.conversation_id != approval.conversation_id:
                raise NotFound(f"action not found: {approval.subject_id}")
            conversation = await self.kernel._require_conversation(unit, approval.conversation_id)
            run = await self._run(unit, action.conversation_id, action.run_id)
            self._stamp(approval, decision)
            await unit.update_approval(approval)
            await self.kernel._append(unit, approval.conversation_id, action.run_id, "approval_decided", {
                "approval_id": approval.id,
                "subject_type": approval.subject_type,
                "subject_id": approval.subject_id,
                "decision": decision,
                "arguments_hash": approval.arguments_hash,
            })
            if decision == "reject":
                await self._reject(unit, action, "user_rejected")
                await self._sync_wait(unit, run)
                return _Recorded(resume=True, conversation_id=action.conversation_id, run_id=action.run_id)
            if run.status in TERMINAL_RUN_STATES or run.status == "paused" or action.run_id in self.pause_requested:
                await unit.update_action_state(action.action_id, state="approved")
                return _Recorded()
            events = await unit.list_events(action.conversation_id)
            checked = evaluate(
                tool_name=action.tool_name,
                arguments=action.arguments,
                mode=conversation.mode,
                strategy=conversation.confirm_strategy,
                config=self.configs.get(action.conversation_id, PolicyConfig()),
                cwd=conversation.cwd,
                events=events,
                cancelled=action.run_id in self.cancel_requested or run.status == "cancelled",
                budget_blocked=_budget_blocked(run, await unit.list_actions(action.conversation_id)),
                confirmed=True,
            )
            if checked.outcome == "deny":
                await self._reject(unit, action, checked.reason)
                await self._sync_wait(unit, run)
                return _Recorded(resume=True, conversation_id=action.conversation_id, run_id=action.run_id)
            await unit.update_action_state(action.action_id, state="executing")
            await self.kernel._append(unit, action.conversation_id, action.run_id, "action_started", {
                "action_id": action.action_id,
                "tool_call_id": action.tool_call_id,
                "tool_name": action.tool_name,
            })
            fresh = await unit.get_run(run.id)
            if fresh.status == "waiting_approval":
                await self._set_status(unit, fresh, "running")
            return _Recorded(job=_job(self, conversation.cwd, action))

    async def _decide_plan(self, unit, approval: Approval, decision: str) -> None:
        conversation = await self.kernel._require_conversation(unit, approval.conversation_id)
        config = self.configs.get(approval.conversation_id, PolicyConfig())
        body = str((approval.arguments or {}).get("body") or "")
        path = str((approval.arguments or {}).get("path") or "")
        accepted = decision == "approve"
        reason = "user_rejected"
        if accepted:
            try:
                _plan_file(conversation.cwd, config.plan_path, path, body)
            except Conflict as exc:
                accepted = False
                reason = "content_changed" if "changed" in str(exc) else "symlink_escape"
        self._stamp(approval, "approve" if accepted else "reject")
        await unit.update_approval(approval)
        active = await unit.get_active_run(approval.conversation_id)
        await self.kernel._append(unit, approval.conversation_id, active.id if active else None, "approval_decided", {
            "approval_id": approval.id,
            "subject_type": "plan",
            "subject_id": approval.subject_id,
            "decision": approval.decision,
            "arguments_hash": approval.arguments_hash,
            "reason": None if accepted else reason,
        })
        if not accepted:
            return
        previous = conversation.mode
        await unit.update_conversation(conversation.id, mode="default")
        await self.kernel._append(unit, conversation.id, active.id if active else None, "mode_changed", {
            "mode": "default",
            "previous": previous,
        })

    async def _commit_result(self, job: Job, result: ToolResult) -> None:
        async with self.kernel.store.transaction() as unit:
            action = await unit.get_action(job.action_id)
            if action is None:
                raise NotFound(f"action not found: {job.action_id}")
            if action.state in TERMINAL_ACTION_STATES:
                return
            state = "outcome_unknown" if result.status == "outcome_unknown" else "finished"
            event = await self.kernel._append(unit, action.conversation_id, action.run_id, "observation", {
                "action_id": action.action_id,
                "tool_call_id": action.tool_call_id,
                "tool_name": action.tool_name,
                "status": result.status,
                "output": result.output,
                "exit_code": result.exit_code,
                "error": result.error,
                "attachments": result.attachments,
                "content_hash": result.content_hash,
                "canonical_path": result.canonical_path,
            })
            await unit.update_action_terminal(action.action_id, state=state, terminal_event_id=event.event_id)

    async def _open_approval(self, unit, conversation_id: str, action: Action) -> Approval:
        approval = Approval(
            id=new_id(),
            conversation_id=conversation_id,
            subject_type="action",
            subject_id=action.action_id,
            decision=None,
            decided_at=None,
            created_at=utc_now(),
            arguments_hash=action.arguments_hash,
            arguments=copy.deepcopy(action.arguments),
            status="pending",
            basis=None,
        )
        await unit.insert_approval(approval)
        await unit.update_action_state(action.action_id, state="waiting_approval")
        return approval

    async def _reject(self, unit, action: Action, reason: str) -> Event | None:
        current = await unit.get_action(action.action_id)
        if current is None or current.state in TERMINAL_ACTION_STATES:
            return None
        event = await self.kernel._append(unit, current.conversation_id, current.run_id, "action_rejected", {
            "action_id": current.action_id,
            "tool_call_id": current.tool_call_id,
            "status": "rejected",
            "reason": reason,
            "output": f"Not executed: {reason}",
        })
        await unit.update_action_terminal(current.action_id, state="rejected", terminal_event_id=event.event_id)
        return event

    async def _unknown(self, unit, action: Action) -> None:
        current = await unit.get_action(action.action_id)
        if current is None or current.state in TERMINAL_ACTION_STATES:
            return
        event = await self.kernel._append(unit, current.conversation_id, current.run_id, "observation", {
            "action_id": current.action_id,
            "tool_call_id": current.tool_call_id,
            "tool_name": current.tool_name,
            "status": "outcome_unknown",
            "output": _UNKNOWN,
            "exit_code": None,
            "error": {"code": "outcome_unknown"},
            "attachments": [],
            "content_hash": None,
            "canonical_path": None,
        })
        await unit.update_action_terminal(current.action_id, state="outcome_unknown", terminal_event_id=event.event_id)

    async def _reject_unstarted(self, unit, actions: list[Action], reason: str) -> None:
        for action in actions:
            if action.state in TERMINAL_ACTION_STATES or action.state == "executing":
                continue
            await self._reject(unit, action, reason)
            approval = await self._pending_for(unit, action.action_id)
            if approval is not None:
                approval.status = "expired"
                await unit.update_approval(approval)

    async def _sync_wait(self, unit, run: Run) -> None:
        current = await unit.get_run(run.id)
        if current is None or current.status in TERMINAL_RUN_STATES or current.status == "paused":
            return
        actions = await unit.list_actions(current.conversation_id)
        waiting = any(action.state == "waiting_approval" and action.run_id == current.id for action in actions)
        desired = "waiting_approval" if waiting else "running"
        if current.status != desired and current.status in {"queued", "running", "waiting_approval"}:
            await self._set_status(unit, current, desired)

    async def _set_status(self, unit, run: Run, status: str) -> Run:
        if run.status == status:
            return run
        if run.status in TERMINAL_RUN_STATES or status not in _ALLOWED.get(run.status, ()):
            raise Conflict(f"cannot change run {run.id} from {run.status} to {status}")
        updated = await unit.update_run(run.id, status=status)
        await self.kernel._append(unit, updated.conversation_id, updated.id, "run_state_changed", {
            "run_id": updated.id,
            "status": status,
        })
        return updated

    async def _run(self, unit, conversation_id: str, run_id: str) -> Run:
        run = await unit.get_run(run_id)
        if run is None or run.conversation_id != conversation_id:
            raise NotFound(f"run not found: {run_id}")
        return run

    async def _approval(self, approval_id: str) -> Approval:
        async with self.kernel.store.transaction() as unit:
            approval = await unit.get_approval(approval_id)
            if approval is None:
                raise NotFound(f"approval not found: {approval_id}")
            return approval

    async def _pending_for(self, unit, action_id: str) -> Approval | None:
        for approval in await unit.list_approvals():
            if approval.subject_type == "action" and approval.subject_id == action_id and approval.status == "pending":
                return approval
        return None

    def _stamp(self, approval: Approval, decision: str) -> None:
        approval.decision = decision
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decided_at = utc_now()


def _job(coordinator: Coordinator, cwd: str, action: Action) -> Job:
    return Job(
        run_id=action.run_id,
        conversation_id=action.conversation_id,
        action_id=action.action_id,
        tool_call_id=action.tool_call_id,
        tool_name=action.tool_name,
        arguments=copy.deepcopy(action.arguments),
        cwd=cwd,
        cancelled=lambda run_id=action.run_id: run_id in coordinator.cancel_requested,
    )


def _ordered(events: list[Event], actions: list[Action], run_id: str) -> list[Action]:
    seq = {
        str(event.payload["action_id"]): event.seq
        for event in events
        if event.event_type == "action"
    }
    chosen = [action for action in actions if action.run_id == run_id]
    return sorted(chosen, key=lambda action: seq.get(action.action_id, 10**12))


def _budget_blocked(run: Run, actions: list[Action]) -> bool:
    budget = run.budget or {}
    limit = budget.get("max_actions")
    if limit is None:
        return False
    used = sum(
        1 for action in actions
        if action.run_id == run.id and action.state in {"executing", "finished", "outcome_unknown"}
    )
    return used >= int(limit)


def _plan_file(cwd: str, plan_path: str | None, submitted: str, body: str) -> tuple[str, str]:
    if not plan_path:
        raise Conflict("plan file is not configured")
    try:
        designated = resolve_inside(cwd, plan_path)
        actual = resolve_inside(cwd, submitted)
    except PathEscape as exc:
        raise Conflict(f"symlink escapes the plan file: {exc}") from exc
    if actual != designated:
        raise Conflict(f"symlink escapes the plan file: {actual}")
    if not actual.is_file():
        raise Conflict("plan file changed")
    digest = file_digest(actual)
    if digest != hashlib.sha256(body.encode("utf-8")).hexdigest():
        raise Conflict("plan file changed")
    return str(actual), digest
