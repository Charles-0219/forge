from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from forge.events import TERMINAL_RUN_STATES, Action, Approval, Conversation, Event, Run
from forge.store import EventStore, UnitOfWork


class _MemoryState:
    def __init__(self) -> None:
        self.conversations: dict[str, Conversation] = {}
        self.runs: dict[str, Run] = {}
        self.events: dict[str, list[Event]] = {}
        self.actions: dict[str, Action] = {}
        self.approvals: dict[str, Approval] = {}


class MemoryUnitOfWork(UnitOfWork):
    def __init__(self, state: _MemoryState, before_write) -> None:
        self._state = state
        self._before_write = before_write

    async def insert_conversation(self, conversation: Conversation) -> Conversation:
        self._before_write()
        self._state.conversations[conversation.id] = conversation
        self._state.events.setdefault(conversation.id, [])
        return conversation

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._state.conversations.get(conversation_id)

    async def insert_run(self, run: Run) -> Run:
        self._before_write()
        self._state.runs[run.id] = run
        return run

    async def get_run(self, run_id: str) -> Run | None:
        return self._state.runs.get(run_id)

    async def get_run_by_request(self, conversation_id: str, client_request_id: str) -> Run | None:
        for run in self._state.runs.values():
            if run.conversation_id == conversation_id and run.client_request_id == client_request_id:
                return run
        return None

    async def get_active_run(self, conversation_id: str) -> Run | None:
        for run in self._state.runs.values():
            if run.conversation_id == conversation_id and run.status not in TERMINAL_RUN_STATES:
                return run
        return None

    async def update_run(self, run_id: str, *, status: str | None = None, usage: dict | None = None) -> Run:
        self._before_write()
        run = self._state.runs[run_id]
        if status is not None:
            run.status = status
        if usage is not None:
            run.usage = usage
        return run

    async def list_runs(self) -> list[Run]:
        return list(self._state.runs.values())

    async def update_conversation(self, conversation_id: str, *, mode: str) -> Conversation:
        self._before_write()
        conversation = self._state.conversations[conversation_id]
        conversation.mode = mode
        return conversation

    async def insert_event(self, event: Event) -> Event:
        self._before_write()
        self._state.events.setdefault(event.conversation_id, []).append(event)
        return event

    async def next_seq(self, conversation_id: str) -> int:
        existing = self._state.events.get(conversation_id) or []
        if not existing:
            return 1
        return max(event.seq for event in existing) + 1

    async def list_events(self, conversation_id: str) -> list[Event]:
        return sorted(self._state.events.get(conversation_id) or [], key=lambda event: event.seq)

    async def insert_action(self, action: Action) -> Action:
        self._before_write()
        self._state.actions[action.action_id] = action
        return action

    async def get_action(self, action_id: str) -> Action | None:
        return self._state.actions.get(action_id)

    async def list_actions(self, conversation_id: str | None = None) -> list[Action]:
        actions = list(self._state.actions.values())
        if conversation_id is None:
            return actions
        return [action for action in actions if action.conversation_id == conversation_id]

    async def update_action_state(self, action_id: str, *, state: str) -> Action:
        self._before_write()
        action = self._state.actions[action_id]
        action.state = state
        return action

    async def update_action_terminal(self, action_id: str, *, state: str, terminal_event_id: str) -> Action:
        self._before_write()
        action = self._state.actions[action_id]
        action.state = state
        action.terminal_event_id = terminal_event_id
        return action

    async def insert_approval(self, approval: Approval) -> Approval:
        self._before_write()
        self._state.approvals[approval.id] = approval
        return approval

    async def get_approval(self, approval_id: str) -> Approval | None:
        return self._state.approvals.get(approval_id)

    async def update_approval(self, approval: Approval) -> Approval:
        self._before_write()
        self._state.approvals[approval.id] = approval
        return approval

    async def list_approvals(self) -> list[Approval]:
        return list(self._state.approvals.values())

    async def count_approvals(self) -> int:
        return len(self._state.approvals)


class MemoryEventStore(EventStore):
    def __init__(self) -> None:
        super().__init__()
        self._state = _MemoryState()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[UnitOfWork]:
        self._writes = 0
        scratch = copy.deepcopy(self._state)
        unit = MemoryUnitOfWork(scratch, self._before_write)
        try:
            yield unit
        except Exception:
            raise
        else:
            self._state = scratch
