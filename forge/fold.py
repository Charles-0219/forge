from __future__ import annotations

from forge.events import Event


_TERMINAL_EVENTS = frozenset({"observation", "action_rejected"})


def summary_text(payload: dict) -> str:
    summary = payload.get("summary") or {}
    return "\n".join([
        "<condensation>",
        str(summary.get("task") or ""),
        str(summary.get("progress") or ""),
        str(summary.get("experience") or ""),
        "</condensation>",
    ])


def build_summary(events: list[Event]) -> dict[str, str]:
    tasks = [str(event.payload.get("body") or "") for event in events if event.event_type == "user_message"]
    tools = [str(event.payload.get("tool_name") or "") for event in events if event.event_type == "action"]
    return {
        "task": tasks[0] if tasks else "",
        "progress": f"folded {len(events)} events",
        "experience": "tools: " + ",".join(tools),
    }


def visible_events(events: list[Event]) -> list[Event]:
    condensations = [event for event in events if event.event_type == "condensation"]
    if not condensations:
        return list(events)
    latest = condensations[-1]
    hidden = {str(item) for item in latest.payload.get("replaces") or []}
    rest = [
        event for event in events
        if event.event_type != "condensation" and event.event_id not in hidden
    ]
    return [latest, *rest]


def fold_targets(events: list[Event]) -> list[Event] | None:
    """Return events a new condensation should replace.

    The newest complete user turn stays, as does every turn that still has an
    unpaired action. A second call with no new completed turn returns None.
    """
    terminals = {
        str(event.payload["action_id"])
        for event in events
        if event.event_type in _TERMINAL_EVENTS and "action_id" in event.payload
    }
    turns = _turns(events)
    if not turns:
        return None
    complete = [index for index, turn in enumerate(turns) if _complete(turn, terminals)]
    if not complete:
        return None
    keep = {event.event_id for event in turns[complete[-1]]}
    for index, turn in enumerate(turns):
        if index != complete[-1] and not _complete(turn, terminals):
            keep.update(event.event_id for event in turn)
    latest = next((event for event in reversed(events) if event.event_type == "condensation"), None)
    already = {str(item) for item in (latest.payload.get("replaces") or [])} if latest else set()
    selected = [
        event for event in events
        if event.event_id not in keep and (latest is None or event.event_id != latest.event_id)
    ]
    new_ids = {event.event_id for event in selected}
    if not new_ids or new_ids <= already:
        return None
    if latest is not None and latest.event_id not in keep:
        selected.append(latest)
    return selected


def _turns(events: list[Event]) -> list[list[Event]]:
    turns: list[list[Event]] = []
    current: list[Event] = []
    for event in events:
        if event.event_type == "user_message" and current:
            turns.append(current)
            current = []
        current.append(event)
    if current:
        turns.append(current)
    return turns


def _complete(turn: list[Event], terminals: set[str]) -> bool:
    action_ids = [str(event.payload["action_id"]) for event in turn if event.event_type == "action"]
    return all(action_id in terminals for action_id in action_ids)
