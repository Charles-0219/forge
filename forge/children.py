from __future__ import annotations

import asyncio
from dataclasses import dataclass

from forge.errors import Conflict, NotFound
from forge.policy import BUILTIN_TOOLS, READ_TOOLS, PolicyConfig
from forge.coordinator import Coordinator


@dataclass(frozen=True)
class ChildSession:
    conversation_id: str
    run_id: str
    root_conversation_id: str
    root_run_id: str
    allow_tools: frozenset[str]
    credential_ref: str
    kind: str


class ChildRuntime:
    """Serial child sessions that share the root budget and cannot nest."""

    def __init__(self, coordinator: Coordinator) -> None:
        self.coordinator = coordinator
        self.credentials: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def spawn(
        self,
        parent_conversation_id: str,
        parent_run_id: str,
        *,
        kind: str,
        prompt: str,
        trigger_action_id: str | None = None,
        child_allow: frozenset[str] | None = None,
        parent_allow: frozenset[str] | None = None,
        credential_ref: str,
        thinking: bool | None = None,
    ) -> ChildSession:
        if kind not in {"explore", "plan", "general", "custom", "skill_fork"}:
            raise Conflict(f"unsupported child kind: {kind}")
        lock = self._locks.setdefault(parent_conversation_id, asyncio.Lock())
        async with lock:
            parent = await self.coordinator.kernel.get_conversation(parent_conversation_id)
            root_run = await self.coordinator.kernel.get_run(parent_run_id)
            if root_run.conversation_id != parent_conversation_id:
                raise NotFound(f"run not found: {parent_run_id}")
            if parent.parent_id is not None:
                raise Conflict("nested child tasks are not allowed")
            await self._require_idle(parent_conversation_id)
            allowed = child_allowlist(kind, parent_allow, child_allow)
            child = await self.coordinator.kernel.create_conversation(
                protocol=parent.protocol,
                model_ref=parent.model_ref,
                cwd=parent.cwd,
                mode="plan" if kind == "plan" else parent.mode,
                confirm_strategy=parent.confirm_strategy,
                parent_id=parent.id,
            )
            run = await self.coordinator.kernel.submit_message(
                child.id,
                body=prompt,
                client_request_id=f"child-{child.id}",
                budget={
                    "kind": kind,
                    "root_conversation_id": parent.id,
                    "root_run_id": parent_run_id,
                    "trigger_action_id": trigger_action_id,
                    "thinking": thinking,
                },
            )
            self.coordinator.configure(child.id, PolicyConfig(
                allow_tools=allowed,
                parent_allow_tools=allowed,
            ))
            self.credentials[child.id] = credential_ref
            return ChildSession(
                conversation_id=child.id,
                run_id=run.id,
                root_conversation_id=parent.id,
                root_run_id=parent_run_id,
                allow_tools=allowed,
                credential_ref=credential_ref,
                kind=kind,
            )

    async def charge(
        self,
        conversation_id: str,
        *,
        calls: int = 1,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        usage_unknown: bool = False,
    ) -> dict:
        kernel = self.coordinator.kernel
        async with kernel.store.transaction() as unit:
            conversation = await unit.get_conversation(conversation_id)
            if conversation is None:
                raise NotFound(f"conversation not found: {conversation_id}")
            child_run = await unit.get_active_run(conversation_id)
            if child_run is None or not child_run.budget:
                raise Conflict("child run has no root budget link")
            root = await unit.get_run(str(child_run.budget["root_run_id"]))
            if root is None:
                raise NotFound("root run not found")
            usage = dict(root.usage or {})
            limits = root.budget or {}
            used_calls = int(usage.get("calls") or 0) + calls
            if limits.get("max_calls") is not None and used_calls > int(limits["max_calls"]):
                raise Conflict("root budget exceeded")
            incomplete = usage_unknown or usage.get("usage_unknown") or input_tokens is None or output_tokens is None
            if incomplete:
                usage["calls"] = used_calls
                usage["usage_unknown"] = True
                await unit.update_run(root.id, usage=usage)
                return usage
            used_tokens = int(usage.get("tokens") or 0) + int(input_tokens) + int(output_tokens)
            if limits.get("max_tokens") is not None and used_tokens > int(limits["max_tokens"]):
                raise Conflict("root budget exceeded")
            usage.update(calls=used_calls, tokens=used_tokens, usage_unknown=False)
            await unit.update_run(root.id, usage=usage)
            return usage

    async def _require_idle(self, parent_conversation_id: str) -> None:
        async with self.coordinator.kernel.store.transaction() as unit:
            for conversation in await unit.list_conversations():
                if conversation.parent_id != parent_conversation_id:
                    continue
                if await unit.get_active_run(conversation.id) is not None:
                    raise Conflict("a child task is already running")


def child_allowlist(
    kind: str,
    parent_allow: frozenset[str] | None,
    child_allow: frozenset[str] | None,
) -> frozenset[str]:
    parent = BUILTIN_TOOLS if parent_allow is None else parent_allow
    if kind in {"explore", "plan"}:
        child = READ_TOOLS if child_allow is None else child_allow
        return parent & child & READ_TOOLS
    child = BUILTIN_TOOLS if child_allow is None else child_allow
    return parent & child
