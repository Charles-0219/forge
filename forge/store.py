from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable

from forge.events import Action, Conversation, Event, Run


class UnitOfWork(ABC):
    @abstractmethod
    async def insert_conversation(self, conversation: Conversation) -> Conversation:
        raise NotImplementedError

    @abstractmethod
    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        raise NotImplementedError

    @abstractmethod
    async def insert_run(self, run: Run) -> Run:
        raise NotImplementedError

    @abstractmethod
    async def get_run(self, run_id: str) -> Run | None:
        raise NotImplementedError

    @abstractmethod
    async def get_run_by_request(self, conversation_id: str, client_request_id: str) -> Run | None:
        raise NotImplementedError

    @abstractmethod
    async def get_active_run(self, conversation_id: str) -> Run | None:
        raise NotImplementedError

    @abstractmethod
    async def update_run(self, run_id: str, *, status: str | None = None, usage: dict | None = None) -> Run:
        raise NotImplementedError

    @abstractmethod
    async def insert_event(self, event: Event) -> Event:
        raise NotImplementedError

    @abstractmethod
    async def next_seq(self, conversation_id: str) -> int:
        raise NotImplementedError

    @abstractmethod
    async def list_events(self, conversation_id: str) -> list[Event]:
        raise NotImplementedError

    @abstractmethod
    async def insert_action(self, action: Action) -> Action:
        raise NotImplementedError

    @abstractmethod
    async def get_action(self, action_id: str) -> Action | None:
        raise NotImplementedError

    @abstractmethod
    async def update_action_terminal(self, action_id: str, *, state: str, terminal_event_id: str) -> Action:
        raise NotImplementedError

    @abstractmethod
    async def count_approvals(self) -> int:
        raise NotImplementedError


class EventStore(ABC):
    def __init__(self) -> None:
        self.fail_after_writes: int | None = None
        self._writes = 0

    def _before_write(self) -> None:
        if self.fail_after_writes is None:
            return
        if self._writes >= self.fail_after_writes:
            raise RuntimeError("injected write failure")
        self._writes += 1

    @abstractmethod
    def transaction(self) -> AsyncIterator[UnitOfWork]:
        raise NotImplementedError

    async def close(self) -> None:
        return None


WriteHook = Callable[[], None]
