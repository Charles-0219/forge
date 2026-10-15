from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    protocol: Mapped[str] = mapped_column(String(32))
    model_ref: Mapped[str] = mapped_column(String(200))
    cwd: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(64))
    confirm_strategy: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("conversation_id", "client_request_id", name="uq_runs_conversation_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    client_request_id: Mapped[str] = mapped_column(String(200))
    message_body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    budget: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_events_conversation_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    seq: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)


class ActionRow(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("conversation_id", "model_call_id", "tool_call_id", name="uq_actions_call"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    model_call_id: Mapped[str] = mapped_column(String(200))
    tool_call_id: Mapped[str] = mapped_column(String(200))
    call_index: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict] = mapped_column(JSON)
    arguments_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    terminal_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(36))
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))
    arguments_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    basis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
