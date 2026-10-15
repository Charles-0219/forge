from __future__ import annotations

import copy
from typing import Any

from forge.adapters import adapt
from forge.errors import Conflict, DuplicateTerminal, NotFound, ProtocolMismatch
from forge.events import (
    SCHEMA_VERSION,
    Action,
    Conversation,
    Event,
    ModelTurn,
    Projection,
    Run,
    new_id,
    utc_now,
)
from forge.hashing import canonical_json, sha256_text, strip_secrets
from forge.projector import logical_request, project_recorded
from forge.store import EventStore


class Kernel:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    async def create_conversation(
        self,
        *,
        protocol: str,
        model_ref: str,
        cwd: str,
        mode: str,
        confirm_strategy: str,
        config_version: str = "0",
        parent_id: str | None = None,
    ) -> Conversation:
        if protocol not in {"openai", "anthropic"}:
            raise ProtocolMismatch(f"unsupported protocol: {protocol}")
        conversation = Conversation(
            id=new_id(),
            protocol=protocol,
            model_ref=model_ref,
            cwd=cwd,
            mode=mode,
            confirm_strategy=confirm_strategy,
            config_version=config_version,
            parent_id=parent_id,
            created_at=utc_now(),
        )
        async with self.store.transaction() as unit:
            if parent_id is not None and await unit.get_conversation(parent_id) is None:
                raise NotFound(f"parent conversation not found: {parent_id}")
            return await unit.insert_conversation(conversation)

    async def submit_message(
        self,
        conversation_id: str,
        *,
        body: str,
        client_request_id: str,
        budget: dict | None = None,
    ) -> Run:
        async with self.store.transaction() as unit:
            if await unit.get_conversation(conversation_id) is None:
                raise NotFound(f"conversation not found: {conversation_id}")
            existing = await unit.get_run_by_request(conversation_id, client_request_id)
            if existing is not None:
                if existing.message_body == body:
                    return existing
                raise Conflict("client_request_id conflicts with a different message")
            if await unit.get_active_run(conversation_id) is not None:
                raise Conflict("conversation has a non-terminal run")
            run = Run(
                id=new_id(),
                conversation_id=conversation_id,
                client_request_id=client_request_id,
                message_body=body,
                status="running",
                budget=copy.deepcopy(budget) if budget is not None else None,
                usage=None,
                error=None,
                created_at=utc_now(),
            )
            await unit.insert_run(run)
            await self._append(unit, conversation_id, run.id, "user_message", {
                "body": body,
                "client_request_id": client_request_id,
            })
            await self._append(unit, conversation_id, run.id, "run_state_changed", {
                "run_id": run.id,
                "status": "running",
            })
            return run

    async def prepare_context(
        self,
        conversation_id: str,
        *,
        system_prompt: str,
        memories: list[dict] | None = None,
        skills: list[dict] | None = None,
        tools: list[dict] | None = None,
        model: dict | None = None,
        config_version: str = "0",
    ) -> Event:
        async with self.store.transaction() as unit:
            conversation = await unit.get_conversation(conversation_id)
            if conversation is None:
                raise NotFound(f"conversation not found: {conversation_id}")
            events = await unit.list_events(conversation_id)
            recorded_skills = []
            for skill in skills or []:
                content = str(skill["content"])
                recorded_skills.append({
                    "version_id": str(skill["version_id"]),
                    "content": content,
                    "content_hash": sha256_text(content),
                })
            fields = {
                "system_prompt": system_prompt,
                "memories": [
                    {"id": str(item["id"]), "content": str(item["content"])}
                    for item in (memories or [])
                ],
                "skills": recorded_skills,
                "tools": copy.deepcopy(tools or []),
                "model": strip_secrets(copy.deepcopy(model or {})),
                "protocol": conversation.protocol,
                "config_version": config_version,
            }
            logical = logical_request(events, fields)
            payload = {
                **fields,
                "until_seq": events[-1].seq if events else 0,
                "projection_rule_version": logical["rule_version"],
                "input_hash": sha256_text(canonical_json(logical)),
            }
            active = await unit.get_active_run(conversation_id)
            return await self._append(unit, conversation_id, active.id if active else None, "context_prepared", payload)

    async def record_response(self, conversation_id: str, run_id: str, response: Any) -> ModelTurn:
        async with self.store.transaction() as unit:
            conversation = await self._require_conversation(unit, conversation_id)
            turn = adapt(conversation.protocol, response)
            await self._record_turn(unit, conversation_id, run_id, turn)
            return turn

    async def settle_action(
        self,
        conversation_id: str,
        action_id: str,
        *,
        kind: str,
        reason: str | None = None,
        output: str | None = None,
    ) -> Event:
        if kind == "finished":
            event_type = "observation"
            status = "finished"
            text = "" if output is None else output
        elif kind == "rejected":
            event_type = "action_rejected"
            status = "rejected"
            text = output or f"Not executed: {reason or 'rejected'}"
            if not text.startswith("Not executed"):
                text = "Not executed: " + text
        elif kind == "outcome_unknown":
            event_type = "observation"
            status = "outcome_unknown"
            text = "Result unknown: the tool outcome could not be confirmed."
        else:
            raise ValueError(f"unsupported action terminal: {kind}")
        async with self.store.transaction() as unit:
            action = await unit.get_action(action_id)
            if action is None or action.conversation_id != conversation_id:
                raise NotFound(f"action not found: {action_id}")
            if action.state != "proposed":
                raise DuplicateTerminal(f"action {action_id} already has a terminal state")
            event = await self._append(unit, conversation_id, action.run_id, event_type, {
                "action_id": action.action_id,
                "tool_call_id": action.tool_call_id,
                "status": status,
                "reason": reason,
                "output": text,
            })
            await unit.update_action_terminal(action_id, state=status, terminal_event_id=event.event_id)
            return event

    async def project(self, conversation_id: str, context_event_id: str | None = None) -> Projection:
        async with self.store.transaction() as unit:
            conversation = await self._require_conversation(unit, conversation_id)
            events = await unit.list_events(conversation_id)
        contexts = [event for event in events if event.event_type == "context_prepared"]
        if context_event_id is None:
            if not contexts:
                raise NotFound("context_prepared event not found")
            context = contexts[-1]
        else:
            matches = [event for event in contexts if event.event_id == context_event_id]
            if not matches:
                raise NotFound(f"context_prepared event not found: {context_event_id}")
            context = matches[0]
        if context.payload["protocol"] != conversation.protocol:
            raise ProtocolMismatch("conversation protocol does not match recorded context")
        return project_recorded(events, context)

    async def list_events(self, conversation_id: str) -> list[Event]:
        async with self.store.transaction() as unit:
            return await unit.list_events(conversation_id)

    async def count_approvals(self) -> int:
        async with self.store.transaction() as unit:
            return await unit.count_approvals()

    async def _record_turn(self, unit, conversation_id: str, run_id: str, turn: ModelTurn) -> None:
        run = await unit.get_run(run_id)
        if run is None or run.conversation_id != conversation_id:
            raise NotFound(f"run not found: {run_id}")
        await self._append(unit, conversation_id, run_id, "assistant_message", {
            "model_call_id": turn.model_call_id,
            "text": turn.text,
            "usage": turn.usage,
        })
        pending: list[tuple[Action, str]] = []
        for proposed in turn.actions:
            action = Action(
                action_id=new_id(),
                conversation_id=conversation_id,
                run_id=run_id,
                model_call_id=turn.model_call_id,
                tool_call_id=proposed.tool_call_id,
                call_index=proposed.call_index,
                tool_name=proposed.tool_name,
                arguments=proposed.arguments,
                arguments_hash=proposed.arguments_hash,
                state="proposed",
                terminal_event_id=None,
            )
            await unit.insert_action(action)
            await self._append(unit, conversation_id, run_id, "action", {
                "action_id": action.action_id,
                "model_call_id": action.model_call_id,
                "tool_call_id": action.tool_call_id,
                "call_index": action.call_index,
                "tool_name": action.tool_name,
                "arguments": action.arguments,
                "arguments_hash": action.arguments_hash,
            })
            if proposed.immediate_rejection:
                pending.append((action, proposed.immediate_rejection))
        for action, reason in pending:
            event = await self._append(unit, conversation_id, run_id, "action_rejected", {
                "action_id": action.action_id,
                "tool_call_id": action.tool_call_id,
                "status": "rejected",
                "reason": reason,
                "output": f"Not executed: {reason}",
            })
            await unit.update_action_terminal(action.action_id, state="rejected", terminal_event_id=event.event_id)
        await unit.update_run(run_id, usage=turn.usage)

    async def _append(self, unit, conversation_id: str, run_id: str | None, event_type: str, payload: dict) -> Event:
        event = Event(
            event_id=new_id(),
            conversation_id=conversation_id,
            seq=await unit.next_seq(conversation_id),
            run_id=run_id,
            event_type=event_type,
            schema_version=SCHEMA_VERSION,
            created_at=utc_now(),
            payload=copy.deepcopy(payload),
        )
        return await unit.insert_event(event)

    async def _require_conversation(self, unit, conversation_id: str) -> Conversation:
        conversation = await unit.get_conversation(conversation_id)
        if conversation is None:
            raise NotFound(f"conversation not found: {conversation_id}")
        return conversation
