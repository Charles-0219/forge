from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.events import TERMINAL_RUN_STATES, Action, Approval, Conversation, Event, Run
from forge.migrate import upgrade
from forge.schema import ActionRow, ApprovalRow, ConversationRow, EventRow, RunRow
from forge.store import EventStore, UnitOfWork


def _conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        id=row.id,
        protocol=row.protocol,
        model_ref=row.model_ref,
        cwd=row.cwd,
        mode=row.mode,
        confirm_strategy=row.confirm_strategy,
        config_version=row.config_version,
        parent_id=row.parent_id,
        created_at=row.created_at,
    )


def _run(row: RunRow) -> Run:
    return Run(
        id=row.id,
        conversation_id=row.conversation_id,
        client_request_id=row.client_request_id,
        message_body=row.message_body,
        status=row.status,
        budget=row.budget,
        usage=row.usage,
        error=row.error,
        created_at=row.created_at,
    )


def _event(row: EventRow) -> Event:
    return Event(
        event_id=row.id,
        conversation_id=row.conversation_id,
        seq=row.seq,
        run_id=row.run_id,
        event_type=row.event_type,
        schema_version=row.schema_version,
        created_at=row.created_at,
        payload=dict(row.payload or {}),
    )


def _action(row: ActionRow) -> Action:
    return Action(
        action_id=row.id,
        conversation_id=row.conversation_id,
        run_id=row.run_id,
        model_call_id=row.model_call_id,
        tool_call_id=row.tool_call_id,
        call_index=row.call_index,
        tool_name=row.tool_name,
        arguments=dict(row.arguments or {}),
        arguments_hash=row.arguments_hash,
        state=row.state,
        terminal_event_id=row.terminal_event_id,
    )


def _approval(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        conversation_id=row.conversation_id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        decision=row.decision,
        decided_at=row.decided_at,
        created_at=row.created_at,
        arguments_hash=row.arguments_hash,
        arguments=dict(row.arguments) if row.arguments is not None else None,
        status=row.status or "pending",
        basis=dict(row.basis) if row.basis is not None else None,
    )


class SqliteUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession, before_write) -> None:
        self._session = session
        self._before_write = before_write

    async def insert_conversation(self, conversation: Conversation) -> Conversation:
        self._before_write()
        self._session.add(ConversationRow(
            id=conversation.id,
            protocol=conversation.protocol,
            model_ref=conversation.model_ref,
            cwd=conversation.cwd,
            mode=conversation.mode,
            confirm_strategy=conversation.confirm_strategy,
            config_version=conversation.config_version,
            parent_id=conversation.parent_id,
            created_at=conversation.created_at,
        ))
        await self._session.flush()
        return conversation

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = await self._session.get(ConversationRow, conversation_id)
        return _conversation(row) if row else None

    async def list_conversations(self) -> list[Conversation]:
        result = await self._session.execute(select(ConversationRow))
        return [_conversation(row) for row in result.scalars()]

    async def insert_run(self, run: Run) -> Run:
        self._before_write()
        self._session.add(RunRow(
            id=run.id,
            conversation_id=run.conversation_id,
            client_request_id=run.client_request_id,
            message_body=run.message_body,
            status=run.status,
            budget=run.budget,
            usage=run.usage,
            error=run.error,
            created_at=run.created_at,
        ))
        await self._session.flush()
        return run

    async def get_run(self, run_id: str) -> Run | None:
        row = await self._session.get(RunRow, run_id)
        return _run(row) if row else None

    async def get_run_by_request(self, conversation_id: str, client_request_id: str) -> Run | None:
        result = await self._session.execute(
            select(RunRow).where(
                RunRow.conversation_id == conversation_id,
                RunRow.client_request_id == client_request_id,
            )
        )
        row = result.scalar_one_or_none()
        return _run(row) if row else None

    async def get_active_run(self, conversation_id: str) -> Run | None:
        result = await self._session.execute(select(RunRow).where(RunRow.conversation_id == conversation_id))
        for row in result.scalars():
            if row.status not in TERMINAL_RUN_STATES:
                return _run(row)
        return None

    async def update_run(self, run_id: str, *, status: str | None = None, usage: dict | None = None) -> Run:
        self._before_write()
        row = await self._session.get(RunRow, run_id)
        if row is None:
            raise KeyError(run_id)
        if status is not None:
            row.status = status
        if usage is not None:
            row.usage = usage
        await self._session.flush()
        return _run(row)

    async def list_runs(self) -> list[Run]:
        result = await self._session.execute(select(RunRow))
        return [_run(row) for row in result.scalars()]

    async def update_conversation(self, conversation_id: str, *, mode: str) -> Conversation:
        self._before_write()
        row = await self._session.get(ConversationRow, conversation_id)
        if row is None:
            raise KeyError(conversation_id)
        row.mode = mode
        await self._session.flush()
        return _conversation(row)

    async def insert_event(self, event: Event) -> Event:
        self._before_write()
        self._session.add(EventRow(
            id=event.event_id,
            conversation_id=event.conversation_id,
            seq=event.seq,
            run_id=event.run_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            created_at=event.created_at,
            payload=event.payload,
        ))
        await self._session.flush()
        return event

    async def next_seq(self, conversation_id: str) -> int:
        result = await self._session.execute(
            select(EventRow.seq).where(EventRow.conversation_id == conversation_id)
        )
        seqs = list(result.scalars())
        return (max(seqs) + 1) if seqs else 1

    async def list_events(self, conversation_id: str) -> list[Event]:
        result = await self._session.execute(
            select(EventRow).where(EventRow.conversation_id == conversation_id).order_by(EventRow.seq)
        )
        return [_event(row) for row in result.scalars()]

    async def insert_action(self, action: Action) -> Action:
        self._before_write()
        self._session.add(ActionRow(
            id=action.action_id,
            conversation_id=action.conversation_id,
            run_id=action.run_id,
            model_call_id=action.model_call_id,
            tool_call_id=action.tool_call_id,
            call_index=action.call_index,
            tool_name=action.tool_name,
            arguments=action.arguments,
            arguments_hash=action.arguments_hash,
            state=action.state,
            terminal_event_id=action.terminal_event_id,
        ))
        await self._session.flush()
        return action

    async def get_action(self, action_id: str) -> Action | None:
        row = await self._session.get(ActionRow, action_id)
        return _action(row) if row else None

    async def list_actions(self, conversation_id: str | None = None) -> list[Action]:
        stmt = select(ActionRow)
        if conversation_id is not None:
            stmt = stmt.where(ActionRow.conversation_id == conversation_id)
        result = await self._session.execute(stmt)
        return [_action(row) for row in result.scalars()]

    async def update_action_state(self, action_id: str, *, state: str) -> Action:
        self._before_write()
        row = await self._session.get(ActionRow, action_id)
        if row is None:
            raise KeyError(action_id)
        row.state = state
        await self._session.flush()
        return _action(row)

    async def update_action_terminal(self, action_id: str, *, state: str, terminal_event_id: str) -> Action:
        self._before_write()
        row = await self._session.get(ActionRow, action_id)
        if row is None:
            raise KeyError(action_id)
        row.state = state
        row.terminal_event_id = terminal_event_id
        await self._session.flush()
        return _action(row)

    async def insert_approval(self, approval: Approval) -> Approval:
        self._before_write()
        self._session.add(ApprovalRow(
            id=approval.id,
            conversation_id=approval.conversation_id,
            subject_type=approval.subject_type,
            subject_id=approval.subject_id,
            decision=approval.decision,
            decided_at=approval.decided_at,
            created_at=approval.created_at,
            arguments_hash=approval.arguments_hash,
            arguments=approval.arguments,
            status=approval.status,
            basis=approval.basis,
        ))
        await self._session.flush()
        return approval

    async def get_approval(self, approval_id: str) -> Approval | None:
        row = await self._session.get(ApprovalRow, approval_id)
        return _approval(row) if row else None

    async def update_approval(self, approval: Approval) -> Approval:
        self._before_write()
        row = await self._session.get(ApprovalRow, approval.id)
        if row is None:
            raise KeyError(approval.id)
        row.decision = approval.decision
        row.decided_at = approval.decided_at
        row.arguments_hash = approval.arguments_hash
        row.arguments = approval.arguments
        row.status = approval.status
        row.basis = approval.basis
        await self._session.flush()
        return _approval(row)

    async def list_approvals(self) -> list[Approval]:
        result = await self._session.execute(select(ApprovalRow))
        return [_approval(row) for row in result.scalars()]

    async def count_approvals(self) -> int:
        result = await self._session.execute(select(ApprovalRow.id))
        return len(list(result.scalars()))


class SqliteEventStore(EventStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = Path(path)

    async def open(self) -> None:
        upgrade(self.path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.path.resolve().as_posix()}")

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self._engine = engine
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[UnitOfWork]:
        self._writes = 0
        async with self._maker() as session:
            async with session.begin():
                yield SqliteUnitOfWork(session, self._before_write)

    async def close(self) -> None:
        await self._engine.dispose()
