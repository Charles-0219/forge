from __future__ import annotations

from typing import Any

from forge.errors import DuplicateTerminal, IncompletePairing
from forge.events import PROJECTION_RULE_VERSION, Event, Projection
from forge.hashing import canonical_json, sha256_text


def compose_system(fields: dict) -> str:
    parts = [str(fields.get("system_prompt") or "")]
    memories = list(fields.get("memories") or [])
    if memories:
        lines = ["<recorded_memory>"]
        for item in memories:
            lines.append(f"{item['id']}\n{item['content']}")
        lines.append("</recorded_memory>")
        parts.append("\n".join(lines))
    skills = list(fields.get("skills") or [])
    if skills:
        lines = ["<recorded_skills>"]
        for item in skills:
            lines.append(f"{item['version_id']}\n{item['content']}")
        lines.append("</recorded_skills>")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def logical_request(events: list[Event], fields: dict) -> dict:
    paired = ensure_paired(events)
    protocol = fields["protocol"]
    return {
        "rule_version": PROJECTION_RULE_VERSION,
        "protocol": protocol,
        "system": compose_system(fields),
        "messages": dialogue(events, protocol, paired),
        "tools": list(fields.get("tools") or []),
        "model": dict(fields.get("model") or {}),
    }


def project_recorded(events: list[Event], context: Event) -> Projection:
    payload = context.payload
    prior = [event for event in events if event.seq <= int(payload["until_seq"])]
    logical = logical_request(prior, payload)
    digest = sha256_text(canonical_json(logical))
    return Projection(
        protocol=logical["protocol"],
        system=logical["system"],
        messages=logical["messages"],
        tools=logical["tools"],
        model=logical["model"],
        input_hash=digest,
        rule_version=PROJECTION_RULE_VERSION,
    )


def ensure_paired(events: list[Event]) -> dict[str, Event]:
    terminals: dict[str, Event] = {}
    for event in events:
        if event.event_type not in {"observation", "action_rejected"}:
            continue
        action_id = str(event.payload["action_id"])
        if action_id in terminals:
            raise DuplicateTerminal(f"action {action_id} has more than one terminal event")
        terminals[action_id] = event
    missing = [
        str(event.payload["action_id"])
        for event in events
        if event.event_type == "action" and str(event.payload["action_id"]) not in terminals
    ]
    if missing:
        raise IncompletePairing("action has no terminal result: " + ", ".join(missing))
    return terminals


def dialogue(events: list[Event], protocol: str, terminals: dict[str, Event]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(events):
        event = events[index]
        if event.event_type == "user_message":
            messages.append({"role": "user", "content": str(event.payload.get("body") or "")})
            index += 1
            continue
        if event.event_type != "assistant_message":
            index += 1
            continue
        call_id = str(event.payload["model_call_id"])
        actions: list[Event] = []
        cursor = index + 1
        while cursor < len(events):
            candidate = events[cursor]
            if candidate.event_type != "action" or str(candidate.payload["model_call_id"]) != call_id:
                break
            actions.append(candidate)
            cursor += 1
        messages.append(_assistant_message(event, actions, protocol))
        if actions:
            results = [_terminal_message(terminals[str(action.payload["action_id"])], protocol) for action in actions]
            if protocol == "openai":
                messages.extend(results)
            else:
                messages.append({"role": "user", "content": results})
        index = cursor
    return messages


def _assistant_message(event: Event, actions: list[Event], protocol: str) -> dict[str, Any]:
    text = str(event.payload.get("text") or "")
    if protocol == "openai":
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if actions:
            message["tool_calls"] = [
                {
                    "id": action.payload["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": action.payload["tool_name"],
                        "arguments": canonical_json(action.payload["arguments"]),
                    },
                }
                for action in actions
            ]
            if text == "":
                message["content"] = None
        return message
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for action in actions:
        blocks.append({
            "type": "tool_use",
            "id": action.payload["tool_call_id"],
            "name": action.payload["tool_name"],
            "input": action.payload["arguments"],
        })
    return {"role": "assistant", "content": blocks}


def _terminal_message(event: Event, protocol: str) -> dict[str, Any]:
    status = str(event.payload["status"])
    output = str(event.payload.get("output") or "")
    failed = status in {"rejected", "outcome_unknown"}
    if protocol == "openai":
        return {"role": "tool", "tool_call_id": event.payload["tool_call_id"], "content": output}
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": event.payload["tool_call_id"],
        "content": output,
    }
    if failed:
        block["is_error"] = True
    return block
