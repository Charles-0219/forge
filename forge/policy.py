from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from forge.events import Event
from forge.paths import PathEscape, file_digest, resolve_inside


BUILTIN_TOOLS = frozenset({
    "read_file",
    "list_files",
    "grep_search",
    "write_file",
    "edit_file",
    "run_shell",
})
READ_TOOLS = frozenset({"read_file", "list_files", "grep_search"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})

_DANGEROUS = (
    re.compile(r"\brm\s"),
    re.compile(r"\bgit\s+(push|reset|clean|checkout\s+\.)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s"),
    re.compile(r">\s*/dev/"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bpkill\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),
    re.compile(r"\bStop-Process\s", re.IGNORECASE),
)

_STRATEGY_ALIASES = {
    "yolo": "never_confirm",
    "--yolo": "never_confirm",
    "dont-ask": "dont_ask",
    "--dont-ask": "dont_ask",
}


@dataclass(frozen=True)
class PolicyConfig:
    allow_tools: frozenset[str] | None = None
    deny_tools: frozenset[str] = frozenset()
    parent_allow_tools: frozenset[str] | None = None
    accept_edits: bool = False
    plan_path: str | None = None


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    risk: str
    hard: bool


def normalize_strategy(strategy: str) -> str:
    return _STRATEGY_ALIASES.get(strategy, strategy)


def intersect_allowlists(parent: frozenset[str] | None, child: frozenset[str] | None) -> frozenset[str]:
    child_set = BUILTIN_TOOLS if child is None else child
    if parent is None:
        return child_set
    return child_set & parent


def is_dangerous(command: str) -> bool:
    return any(pattern.search(command) for pattern in _DANGEROUS)


def classify_risk(tool_name: str, arguments: dict) -> str:
    if tool_name in READ_TOOLS:
        return "low"
    if tool_name in WRITE_TOOLS:
        return "medium"
    if tool_name == "run_shell":
        return "high" if is_dangerous(str(arguments.get("command") or "")) else "unknown"
    return "unknown"


def last_read_hash(events: list[Event], canonical_path: str) -> str | None:
    found = None
    for event in events:
        if event.event_type != "observation":
            continue
        payload = event.payload
        if payload.get("tool_name") != "read_file" or payload.get("status") != "success":
            continue
        if payload.get("canonical_path") != canonical_path:
            continue
        found = payload.get("content_hash")
    return found


def evaluate(
    *,
    tool_name: str,
    arguments: dict,
    mode: str,
    strategy: str,
    config: PolicyConfig,
    cwd: str,
    events: list[Event],
    cancelled: bool,
    budget_blocked: bool,
    confirmed: bool = False,
) -> Decision:
    """Hard checks run before risk and confirmation.

    ``confirmed`` repeats only the hard checks. An approval cannot lift a hard deny.
    """
    invalid = _validate(tool_name, arguments)
    if invalid is not None:
        return Decision("deny", invalid, "unknown", True)
    risk = classify_risk(tool_name, arguments)
    denied = _hard_constraints(
        tool_name=tool_name,
        arguments=arguments,
        mode=mode,
        config=config,
        cwd=cwd,
        events=events,
        cancelled=cancelled,
        budget_blocked=budget_blocked,
        risk=risk,
    )
    if denied is not None:
        return denied
    if confirmed:
        return Decision("execute", "approved", risk, False)
    return _confirm(tool_name, risk, normalize_strategy(strategy), config)


def _validate(tool_name: str, arguments: dict) -> str | None:
    if tool_name not in BUILTIN_TOOLS:
        return "unsupported_tool"
    if not isinstance(arguments, dict):
        return "invalid_arguments"
    if tool_name == "read_file":
        return _text(arguments, "file_path")
    if tool_name == "write_file":
        return _text(arguments, "file_path") or _text(arguments, "content", allow_empty=True)
    if tool_name == "edit_file":
        return (
            _text(arguments, "file_path")
            or _text(arguments, "old_string")
            or _text(arguments, "new_string", allow_empty=True)
        )
    if tool_name in {"list_files", "grep_search"}:
        if _text(arguments, "pattern") is not None:
            return "invalid_arguments"
        if tool_name == "grep_search":
            try:
                re.compile(str(arguments["pattern"]))
            except re.error:
                return "invalid_arguments"
        return None
    if tool_name == "run_shell":
        if _text(arguments, "command") is not None:
            return "invalid_arguments"
        if "timeout" in arguments and not isinstance(arguments["timeout"], (int, float)):
            return "invalid_arguments"
        return None
    return "unsupported_tool"


def _text(arguments: dict, key: str, *, allow_empty: bool = False) -> str | None:
    value = arguments.get(key)
    if not isinstance(value, str):
        return "invalid_arguments"
    if value == "" and not allow_empty:
        return "invalid_arguments"
    return None


def _hard_constraints(
    *,
    tool_name: str,
    arguments: dict,
    mode: str,
    config: PolicyConfig,
    cwd: str,
    events: list[Event],
    cancelled: bool,
    budget_blocked: bool,
    risk: str,
) -> Decision | None:
    if tool_name in config.deny_tools:
        return Decision("deny", "explicit_deny", risk, True)
    path_decision = _path_constraints(tool_name, arguments, mode, config, cwd, events, risk)
    if path_decision is not None:
        return path_decision
    allowed = intersect_allowlists(config.parent_allow_tools, config.allow_tools)
    if tool_name not in allowed:
        return Decision("deny", "tool_not_allowed", risk, True)
    hashed = _content_hash(tool_name, arguments, cwd, events, risk)
    if hashed is not None:
        return hashed
    if budget_blocked:
        return Decision("deny", "budget_exceeded", risk, True)
    if cancelled:
        return Decision("deny", "cancelled", risk, True)
    return None


def _path_constraints(tool_name, arguments, mode, config: PolicyConfig, cwd: str, events, risk) -> Decision | None:
    del events
    if tool_name == "run_shell":
        if mode == "plan":
            return Decision("deny", "plan_mode", risk, True)
        return None
    raw_paths = []
    if tool_name in {"read_file", "write_file", "edit_file"}:
        raw_paths.append(str(arguments["file_path"]))
    elif arguments.get("path"):
        raw_paths.append(str(arguments["path"]))
    resolved: dict[str, Path] = {}
    for raw in raw_paths:
        try:
            resolved[raw] = resolve_inside(cwd, raw)
        except PathEscape:
            return Decision("deny", "symlink_escape", risk, True)
    if mode == "plan" and tool_name in WRITE_TOOLS:
        if not config.plan_path:
            return Decision("deny", "plan_mode", risk, True)
        try:
            designated = resolve_inside(cwd, config.plan_path)
        except PathEscape:
            return Decision("deny", "symlink_escape", risk, True)
        actual = resolved.get(str(arguments["file_path"]))
        if actual != designated:
            return Decision("deny", "plan_mode", risk, True)
    return None


def _content_hash(tool_name, arguments, cwd: str, events, risk) -> Decision | None:
    if tool_name not in WRITE_TOOLS:
        return None
    try:
        path = resolve_inside(cwd, str(arguments["file_path"]))
    except PathEscape:
        return Decision("deny", "symlink_escape", risk, True)
    if not path.exists():
        if tool_name == "edit_file":
            return Decision("deny", "not_read", risk, True)
        return None
    if not path.is_file():
        return Decision("deny", "invalid_arguments", risk, True)
    previous = last_read_hash(events, str(path))
    if previous is None:
        return Decision("deny", "not_read", risk, True)
    if previous != file_digest(path):
        return Decision("deny", "content_changed", risk, True)
    return None


def _confirm(tool_name: str, risk: str, strategy: str, config: PolicyConfig) -> Decision:
    if strategy == "always_confirm":
        return Decision("confirm", risk, risk, False)
    if config.accept_edits and tool_name in WRITE_TOOLS and risk == "medium":
        return Decision("execute", "accept_edits", risk, False)
    if strategy == "never_confirm":
        return Decision("execute", "never_confirm", risk, False)
    needs_confirmation = risk in {"high", "unknown"}
    if strategy == "dont_ask":
        if needs_confirmation:
            return Decision("deny", "auto_denied", risk, False)
        return Decision("execute", "dont_ask", risk, False)
    if needs_confirmation:
        return Decision("confirm", risk, risk, False)
    return Decision("execute", "confirm_risky", risk, False)
