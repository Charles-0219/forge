from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable

from forge.events import Action, Approval, Conversation, Event, Run
from forge.skill_records import EvalReport, Skill, SkillVersion


class UnitOfWork(ABC):
    @abstractmethod
    async def insert_conversation(self, conversation: Conversation) -> Conversation:
        raise NotImplementedError

    @abstractmethod
    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        raise NotImplementedError

    @abstractmethod
    async def list_conversations(self) -> list[Conversation]:
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
    async def list_runs(self) -> list[Run]:
        raise NotImplementedError

    @abstractmethod
    async def update_conversation(self, conversation_id: str, *, mode: str) -> Conversation:
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
    async def list_actions(self, conversation_id: str | None = None) -> list[Action]:
        raise NotImplementedError

    @abstractmethod
    async def update_action_state(self, action_id: str, *, state: str) -> Action:
        raise NotImplementedError

    @abstractmethod
    async def update_action_terminal(self, action_id: str, *, state: str, terminal_event_id: str) -> Action:
        raise NotImplementedError

    @abstractmethod
    async def insert_approval(self, approval: Approval) -> Approval:
        raise NotImplementedError

    @abstractmethod
    async def get_approval(self, approval_id: str) -> Approval | None:
        raise NotImplementedError

    @abstractmethod
    async def update_approval(self, approval: Approval) -> Approval:
        raise NotImplementedError

    @abstractmethod
    async def list_approvals(self) -> list[Approval]:
        raise NotImplementedError

    @abstractmethod
    async def count_approvals(self) -> int:
        raise NotImplementedError

    @abstractmethod
    async def insert_skill(self, skill: Skill) -> Skill:
        raise NotImplementedError

    @abstractmethod
    async def get_skill(self, skill_id: str) -> Skill | None:
        raise NotImplementedError

    @abstractmethod
    async def get_skill_by_name(self, scope: str, name: str) -> Skill | None:
        raise NotImplementedError

    @abstractmethod
    async def list_skills(self) -> list[Skill]:
        raise NotImplementedError

    @abstractmethod
    async def update_skill(self, skill: Skill) -> Skill:
        raise NotImplementedError

    @abstractmethod
    async def insert_skill_version(self, version: SkillVersion) -> SkillVersion:
        raise NotImplementedError

    @abstractmethod
    async def get_skill_version(self, version_id: str) -> SkillVersion | None:
        raise NotImplementedError

    @abstractmethod
    async def list_skill_versions(self, skill_id: str | None = None) -> list[SkillVersion]:
        raise NotImplementedError

    @abstractmethod
    async def update_skill_version(self, version: SkillVersion) -> SkillVersion:
        raise NotImplementedError

    @abstractmethod
    async def insert_eval_report(self, report: EvalReport) -> EvalReport:
        raise NotImplementedError

    @abstractmethod
    async def get_eval_report(self, report_id: str) -> EvalReport | None:
        raise NotImplementedError

    @abstractmethod
    async def list_eval_reports(self) -> list[EvalReport]:
        raise NotImplementedError

    @abstractmethod
    async def update_eval_report(self, report: EvalReport) -> EvalReport:
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
