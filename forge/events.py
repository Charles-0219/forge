from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1
PROJECTION_RULE_VERSION = "phase2.v1"

EVENT_TYPES = frozenset({
    "user_message",
    "context_prepared",
    "assistant_message",
    "action",
    "action_started",
    "observation",
    "action_rejected",
    "approval_decided",
    "condensation",
    "run_state_changed",
    "mode_changed",
    "skill_proposed",
    "skill_decided",
    "skill_eval_completed",
    "skill_promoted",
})

TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
TERMINAL_ACTION_STATES = frozenset({"finished", "rejected", "outcome_unknown"})


def new_id() -> str:
    return str(uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Conversation:
    id: str
    protocol: str
    model_ref: str
    cwd: str
    mode: str
    confirm_strategy: str
    config_version: str
    parent_id: str | None
    created_at: str


@dataclass
class Run:
    id: str
    conversation_id: str
    client_request_id: str
    message_body: str
    status: str
    budget: dict | None
    usage: dict | None
    error: str | None
    created_at: str


@dataclass
class Event:
    event_id: str
    conversation_id: str
    seq: int
    run_id: str | None
    event_type: str
    schema_version: int
    created_at: str
    payload: dict[str, Any]


@dataclass
class Action:
    action_id: str
    conversation_id: str
    run_id: str
    model_call_id: str
    tool_call_id: str
    call_index: int
    tool_name: str
    arguments: dict
    arguments_hash: str
    state: str
    terminal_event_id: str | None


@dataclass
class ProposedAction:
    tool_call_id: str
    call_index: int
    tool_name: str
    arguments: dict
    arguments_hash: str
    immediate_rejection: str | None = None


@dataclass
class ModelTurn:
    protocol: str
    model_call_id: str
    text: str
    actions: list[ProposedAction]
    usage: dict


@dataclass
class Projection:
    protocol: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict]
    model: dict
    input_hash: str
    rule_version: str
