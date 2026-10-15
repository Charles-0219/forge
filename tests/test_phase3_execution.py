from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from forge.coordinator import Coordinator
from forge.errors import Conflict, Gone
from forge.events import Event
from forge.fold import visible_events
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from forge.policy import PolicyConfig, evaluate, intersect_allowlists
from forge.projector import _terminal_message
from forge.sqlite_store import SqliteEventStore


@pytest.fixture(params=["memory", "sqlite"])
async def kernel(request, tmp_path):
    if request.param == "memory":
        yield Kernel(MemoryEventStore())
        return
    store = SqliteEventStore(tmp_path / "phase3.sqlite")
    await store.open()
    try:
        yield Kernel(store)
    finally:
        await store.close()


def model_response(calls, response_id):
    tool_calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        for index, (name, arguments) in enumerate(calls)
    ]
    return {
        "id": response_id,
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


async def open_run(kernel, work, *, mode="default", strategy="confirm_risky", budget=None, body="synthetic task"):
    work.mkdir(parents=True, exist_ok=True)
    conversation = await kernel.create_conversation(
        protocol="openai",
        model_ref="deepseek-flash",
        cwd=str(work),
        mode=mode,
        confirm_strategy=strategy,
    )
    run = await kernel.submit_message(conversation.id, body=body, client_request_id=f"req-{body}", budget=budget)
    return conversation, run


async def propose(kernel, conversation_id, run_id, calls, response_id):
    await kernel.record_response(conversation_id, run_id, model_response(calls, response_id))


async def approvals_of(kernel):
    async with kernel.store.transaction() as unit:
        return await unit.list_approvals()


def reasons(events):
    return [event.payload.get("reason") for event in events if event.event_type == "action_rejected"]


async def test_hard_deny_beats_allow_yolo_and_accept_edits(kernel, tmp_path):
    denied = evaluate(
        tool_name="write_file",
        arguments={"file_path": "note.txt", "content": "x"},
        mode="default",
        strategy="yolo",
        config=PolicyConfig(
            allow_tools=frozenset({"write_file"}),
            deny_tools=frozenset({"write_file"}),
            accept_edits=True,
        ),
        cwd=str(tmp_path),
        events=[],
        cancelled=False,
        budget_blocked=False,
        confirmed=True,
    )
    assert denied.outcome == "deny" and denied.hard and denied.reason == "explicit_deny"
    planned = evaluate(
        tool_name="run_shell",
        arguments={"command": "echo hi"},
        mode="plan",
        strategy="never_confirm",
        config=PolicyConfig(accept_edits=True),
        cwd=str(tmp_path),
        events=[],
        cancelled=False,
        budget_blocked=False,
        confirmed=True,
    )
    assert planned.outcome == "deny" and planned.hard and planned.reason == "plan_mode"
    auto_denied = evaluate(
        tool_name="run_shell",
        arguments={"command": "echo kill"},
        mode="default",
        strategy="dont_ask",
        config=PolicyConfig(),
        cwd=str(tmp_path),
        events=[],
        cancelled=False,
        budget_blocked=False,
    )
    assert auto_denied.outcome == "deny" and auto_denied.hard is False

    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="yolo")
    coordinator = Coordinator(kernel)
    coordinator.configure(conversation.id, PolicyConfig(
        allow_tools=frozenset({"write_file", "run_shell"}),
        deny_tools=frozenset({"write_file"}),
        accept_edits=True,
    ))
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "note.txt", "content": "secret"})], "deny-1")
    await coordinator.dispatch(conversation.id, run.id)
    assert not (work / "note.txt").exists()
    assert reasons(await kernel.list_events(conversation.id)) == ["explicit_deny"]
    assert await kernel.count_approvals() == 0

    other, other_run = await open_run(kernel, tmp_path / "other", strategy="yolo", body="other")
    coordinator.configure(other.id, PolicyConfig())
    await propose(kernel, other.id, other_run.id, [("run_shell", {"command": "echo kill"})], "yolo-1")
    await coordinator.dispatch(other.id, other_run.id)
    observation = next(event for event in await kernel.list_events(other.id) if event.event_type == "observation")
    assert observation.payload["status"] == "success"
    assert "kill" in observation.payload["output"]
    assert [item for item in await approvals_of(kernel) if item.conversation_id == other.id] == []


async def test_plan_mode_rejects_shell(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, mode="plan", strategy="never_confirm")
    coordinator = Coordinator(kernel)
    coordinator.configure(conversation.id, PolicyConfig(plan_path="PLAN.md"))
    await propose(kernel, conversation.id, run.id, [(
        "run_shell",
        {"command": "echo ran > marker.txt"},
    )], "plan-shell")
    await coordinator.dispatch(conversation.id, run.id)
    assert not (work / "marker.txt").exists()
    assert reasons(await kernel.list_events(conversation.id)) == ["plan_mode"]
    assert await kernel.count_approvals() == 0


async def test_repeat_approval_does_not_execute_twice(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="always_confirm")
    coordinator = Coordinator(kernel)
    calls = {"n": 0}
    original = coordinator.dispatcher.run

    async def counting(job):
        calls["n"] += 1
        return await original(job)

    coordinator.dispatcher.run = counting
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "note.txt", "content": "once"})], "once")
    await coordinator.dispatch(conversation.id, run.id)
    approval = (await approvals_of(kernel))[0]
    first = await coordinator.decide(approval.id, "approve", approval.arguments_hash)
    second = await coordinator.decide(approval.id, "approve", approval.arguments_hash)
    assert first.id == second.id and second.decision == "approve"
    assert (work / "note.txt").read_text(encoding="utf-8") == "once"
    assert calls["n"] == 1
    observations = [event for event in await kernel.list_events(conversation.id) if event.event_type == "observation"]
    assert len(observations) == 1
    with pytest.raises(Conflict) as conflict:
        await coordinator.decide(approval.id, "reject", approval.arguments_hash)
    assert conflict.value.code == 409
    assert calls["n"] == 1


async def test_expired_approval_returns_410(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="always_confirm")
    coordinator = Coordinator(kernel)
    calls = {"n": 0}
    original = coordinator.dispatcher.run

    async def counting(job):
        calls["n"] += 1
        return await original(job)

    coordinator.dispatcher.run = counting
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "note.txt", "content": "nope"})], "expire")
    await coordinator.dispatch(conversation.id, run.id)
    approval = (await approvals_of(kernel))[0]
    before = len(await kernel.list_events(conversation.id))
    first = await coordinator.recover()
    assert first["changes"] > 0
    assert not (work / "note.txt").exists()
    assert calls["n"] == 0
    with pytest.raises(Gone) as gone:
        await coordinator.decide(approval.id, "approve", approval.arguments_hash)
    assert gone.value.code == 410
    second = await coordinator.recover()
    assert second["changes"] == 0
    assert len(await kernel.list_events(conversation.id)) == before + 2
    assert calls["n"] == 0
    with pytest.raises(Conflict):
        await coordinator.resume(conversation.id, run.id)


async def test_file_change_after_approval_rejects_the_action(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="always_confirm")
    coordinator = Coordinator(kernel)
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "note.txt", "content": "one"})], "create")
    await coordinator.dispatch(conversation.id, run.id)
    created = next(item for item in await approvals_of(kernel) if item.status == "pending")
    await coordinator.decide(created.id, "approve", created.arguments_hash)
    await propose(kernel, conversation.id, run.id, [(
        "edit_file",
        {"file_path": "note.txt", "old_string": "one", "new_string": "two"},
    )], "edit-early")
    await coordinator.dispatch(conversation.id, run.id)
    assert (work / "note.txt").read_text(encoding="utf-8") == "one"
    assert "not_read" in reasons(await kernel.list_events(conversation.id))

    await propose(kernel, conversation.id, run.id, [("read_file", {"file_path": "note.txt"})], "read")
    await coordinator.dispatch(conversation.id, run.id)
    read_approval = next(item for item in await approvals_of(kernel) if item.status == "pending")
    await coordinator.decide(read_approval.id, "approve", read_approval.arguments_hash)
    await propose(kernel, conversation.id, run.id, [(
        "edit_file",
        {"file_path": "note.txt", "old_string": "one", "new_string": "two"},
    )], "edit")
    await coordinator.dispatch(conversation.id, run.id)
    edit_approval = next(item for item in await approvals_of(kernel) if item.status == "pending")
    (work / "note.txt").write_text("mutated", encoding="utf-8")
    decided = await coordinator.decide(edit_approval.id, "approve", edit_approval.arguments_hash)
    assert decided.decision == "approve"
    assert (work / "note.txt").read_text(encoding="utf-8") == "mutated"
    assert "content_changed" in reasons(await kernel.list_events(conversation.id))
    await coordinator.decide(edit_approval.id, "approve", edit_approval.arguments_hash)
    assert (work / "note.txt").read_text(encoding="utf-8") == "mutated"


async def test_plan_changes_and_symlinks_stay_in_plan_mode(kernel, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-PHASE3", encoding="utf-8")
    link = work / "alias.md"
    link.symlink_to(secret)
    (work / "PLAN.md").write_text("version one", encoding="utf-8")
    conversation = await kernel.create_conversation(
        protocol="openai",
        model_ref="deepseek-flash",
        cwd=str(work),
        mode="plan",
        confirm_strategy="always_confirm",
    )
    coordinator = Coordinator(kernel)
    coordinator.configure(conversation.id, PolicyConfig(plan_path="PLAN.md"))
    with pytest.raises(Conflict):
        await coordinator.submit_plan(conversation.id, body="version one", path="alias.md")
    with pytest.raises(Conflict):
        await coordinator.submit_plan(conversation.id, body="version one", path="../secret.txt")
    assert (await kernel.get_conversation(conversation.id)).mode == "plan"

    approval = await coordinator.submit_plan(conversation.id, body="version one", path="PLAN.md")
    kernel.store.fail_after_writes = 0
    with pytest.raises(RuntimeError, match="injected write failure"):
        await coordinator.decide(approval.id, "approve", approval.arguments_hash)
    kernel.store.fail_after_writes = None
    assert (await kernel.get_conversation(conversation.id)).mode == "plan"
    assert not any(event.event_type == "mode_changed" for event in await kernel.list_events(conversation.id))
    assert (await approvals_of(kernel))[0].status == "pending"

    (work / "PLAN.md").write_text("version two", encoding="utf-8")
    changed = await coordinator.decide(approval.id, "approve", approval.arguments_hash)
    assert changed.decision == "reject"
    assert (await kernel.get_conversation(conversation.id)).mode == "plan"
    decided = next(event for event in await kernel.list_events(conversation.id) if event.event_type == "approval_decided")
    assert decided.payload["reason"] == "content_changed"
    assert not any(event.event_type == "mode_changed" for event in await kernel.list_events(conversation.id))

    fresh = await kernel.create_conversation(
        protocol="openai",
        model_ref="deepseek-flash",
        cwd=str(work),
        mode="plan",
        confirm_strategy="always_confirm",
    )
    (work / "PLAN.md").write_text("ready", encoding="utf-8")
    coordinator.configure(fresh.id, PolicyConfig(plan_path="PLAN.md"))
    ready = await coordinator.submit_plan(fresh.id, body="ready", path="PLAN.md")
    accepted = await coordinator.decide(ready.id, "approve", ready.arguments_hash)
    assert accepted.decision == "approve"
    assert (await kernel.get_conversation(fresh.id)).mode == "default"
    kinds = [event.event_type for event in await kernel.list_events(fresh.id)]
    assert "approval_decided" in kinds and "mode_changed" in kinds
    rendered = json.dumps([event.payload for event in await kernel.list_events(conversation.id)], ensure_ascii=False)
    assert "TOP-SECRET-PHASE3" not in rendered


async def test_shell_stays_responsive_and_can_time_out(kernel, tmp_path):
    shell_work = tmp_path / "shell"
    other_work = tmp_path / "other"
    conversation, run = await open_run(kernel, shell_work, strategy="never_confirm", body="shell")
    other, other_run = await open_run(kernel, other_work, strategy="always_confirm", body="other")
    coordinator = Coordinator(kernel)
    coordinator.configure(conversation.id, PolicyConfig())
    coordinator.configure(other.id, PolicyConfig())
    await propose(kernel, other.id, other_run.id, [("write_file", {"file_path": "b.txt", "content": "b"})], "other-write")
    await coordinator.dispatch(other.id, other_run.id)
    approval = next(item for item in await approvals_of(kernel) if item.conversation_id == other.id)
    await propose(kernel, conversation.id, run.id, [("run_shell", {"command": "sleep 1", "timeout": 5000})], "sleep")
    task = asyncio.create_task(coordinator.dispatch(conversation.id, run.id))
    approved_during_sleep = False
    for _ in range(50):
        await asyncio.sleep(0.05)
        started = any(event.event_type == "action_started" for event in await kernel.list_events(conversation.id))
        if started and not task.done():
            current = await kernel.get_run(run.id)
            assert current.status == "running"
            await coordinator.decide(approval.id, "approve", approval.arguments_hash)
        if (other_work / "b.txt").exists() and not task.done():
            approved_during_sleep = True
            break
    await asyncio.wait_for(task, timeout=5)
    assert approved_during_sleep
    assert (other_work / "b.txt").read_text(encoding="utf-8") == "b"
    observation = next(event for event in await kernel.list_events(conversation.id) if event.event_type == "observation")
    assert observation.payload["status"] == "success"

    await coordinator.finish(conversation.id, run.id)
    timeout_run = await kernel.submit_message(conversation.id, body="timeout", client_request_id="timeout")
    await propose(kernel, conversation.id, timeout_run.id, [("run_shell", {"command": "sleep 5", "timeout": 400})], "timeout")
    started = time.monotonic()
    await coordinator.dispatch(conversation.id, timeout_run.id)
    assert time.monotonic() - started < 3
    timed = next(event for event in await kernel.list_events(conversation.id) if event.event_type == "observation" and event.payload.get("error"))
    assert timed.payload["status"] == "error"
    assert timed.payload["error"]["code"] == "timeout"


async def test_crash_before_execute_does_not_run_the_tool(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel)
    calls = {"n": 0}
    original = coordinator.dispatcher.run

    async def counting(job):
        calls["n"] += 1
        return await original(job)

    coordinator.dispatcher.run = counting
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "note.txt", "content": "ran"})], "before")
    kernel.store.fail_after_writes = 0
    with pytest.raises(RuntimeError, match="injected write failure"):
        await coordinator.dispatch(conversation.id, run.id)
    kernel.store.fail_after_writes = None
    assert calls["n"] == 0
    assert not (work / "note.txt").exists()
    await coordinator.recover()
    await coordinator.dispatch(conversation.id, run.id)
    assert calls["n"] == 0
    assert not (work / "note.txt").exists()
    assert (await coordinator.recover())["changes"] == 0
    assert "interrupted before execution" in reasons(await kernel.list_events(conversation.id))


async def test_crash_after_execute_does_not_rerun_the_tool(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel)
    calls = {"n": 0}
    original = coordinator.dispatcher.run

    async def counting(job):
        calls["n"] += 1
        return await original(job)

    coordinator.dispatcher.run = counting

    def fail_observation():
        kernel.store.fail_after_writes = 0

    coordinator.after_execute = fail_observation
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "note.txt", "content": "ran"})], "after")
    with pytest.raises(RuntimeError, match="injected write failure"):
        await coordinator.dispatch(conversation.id, run.id)
    kernel.store.fail_after_writes = None
    assert calls["n"] == 1
    assert (work / "note.txt").read_text(encoding="utf-8") == "ran"
    with pytest.raises(Conflict):
        await coordinator.dispatch(conversation.id, run.id)
    assert calls["n"] == 1
    before = len(await kernel.list_events(conversation.id))
    await coordinator.recover()
    assert calls["n"] == 1
    assert (work / "note.txt").read_text(encoding="utf-8") == "ran"
    unknown = next(event for event in await kernel.list_events(conversation.id) if event.event_type == "observation")
    assert unknown.payload["status"] == "outcome_unknown"
    assert "could not be confirmed" in unknown.payload["output"]
    assert "success" not in unknown.payload["output"].lower()
    assert (await coordinator.recover())["changes"] == 0
    assert len(await kernel.list_events(conversation.id)) == before + 2
    await coordinator.dispatch(conversation.id, run.id)
    assert calls["n"] == 1


async def test_fold_keeps_unfinished_actions_and_one_summary(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm", body="turn-1")
    coordinator = Coordinator(kernel)
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "one.txt", "content": "turn-1"})], "fold-1")
    await coordinator.dispatch(conversation.id, run.id)
    await coordinator.finish(conversation.id, run.id)
    second = await kernel.submit_message(conversation.id, body="turn-2", client_request_id="fold-2-msg")
    await propose(kernel, conversation.id, second.id, [("write_file", {"file_path": "two.txt", "content": "turn-2"})], "fold-2")
    await coordinator.dispatch(conversation.id, second.id)
    await coordinator.finish(conversation.id, second.id)
    third = await kernel.submit_message(conversation.id, body="turn-3", client_request_id="fold-3-msg")
    await propose(kernel, conversation.id, third.id, [("write_file", {"file_path": "three.txt", "content": "turn-3"})], "fold-3")
    folded = await coordinator.fold(conversation.id)
    assert folded is not None
    replaced = set(folded.payload["replaces"])
    events = await kernel.list_events(conversation.id)
    turn1 = next(event.event_id for event in events if event.event_type == "user_message" and event.payload["body"] == "turn-1")
    turn3_action = next(event for event in events if event.event_type == "action" and event.payload["tool_call_id"] == "call-0" and event.run_id == third.id)
    assert turn1 in replaced
    assert turn3_action.event_id not in replaced
    assert folded.payload["summary"]["task"] == "turn-1"
    assert folded.payload["generator"]["name"] == "phase3.fold"
    assert await coordinator.fold(conversation.id) is None
    assert [event.event_type for event in await kernel.list_events(conversation.id)].count("condensation") == 1

    visible = visible_events(await kernel.list_events(conversation.id))
    visible_actions = {event.payload["action_id"] for event in visible if event.event_type == "action"}
    assert turn3_action.payload["action_id"] in visible_actions
    await kernel.settle_action(conversation.id, turn3_action.payload["action_id"], kind="finished", output="kept")
    prepared = await kernel.prepare_context(conversation.id, system_prompt="stable system")
    projection = await kernel.project(conversation.id, prepared.event_id)
    user_text = [message["content"] for message in projection.messages if message.get("role") == "user" and isinstance(message.get("content"), str)]
    assert user_text.count("turn-1") == 0
    assert sum("<condensation>" in text and "turn-1" in text for text in user_text) == 1
    assert "turn-2" in user_text and "turn-3" in user_text
    assert any(message.get("content") == "kept" for message in projection.messages)
    visible = visible_events(await kernel.list_events(conversation.id))
    actions = {event.payload["action_id"] for event in visible if event.event_type == "action"}
    terminals = {
        event.payload["action_id"]
        for event in visible
        if event.event_type in {"observation", "action_rejected"}
    }
    assert actions == terminals
    assert actions


async def test_parent_allowlist_is_an_intersection(kernel, tmp_path):
    assert intersect_allowlists(frozenset({"read_file"}), None) == frozenset({"read_file"})
    work = tmp_path / "work"
    (work).mkdir()
    (work / "note.txt").write_text("kept", encoding="utf-8")
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel)
    coordinator.configure(conversation.id, PolicyConfig(parent_allow_tools=frozenset({"read_file"})))
    await propose(kernel, conversation.id, run.id, [
        ("write_file", {"file_path": "new.txt", "content": "no"}),
        ("read_file", {"file_path": "note.txt"}),
    ], "intersect")
    await coordinator.dispatch(conversation.id, run.id)
    assert not (work / "new.txt").exists()
    assert "tool_not_allowed" in reasons(await kernel.list_events(conversation.id))
    observation = next(event for event in await kernel.list_events(conversation.id) if event.event_type == "observation")
    assert observation.payload["status"] == "success"
    assert "kept" in observation.payload["output"]


async def test_pause_blocks_an_approval_until_resume(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="always_confirm")
    coordinator = Coordinator(kernel)
    await propose(kernel, conversation.id, run.id, [("write_file", {"file_path": "note.txt", "content": "later"})], "pause")
    await coordinator.dispatch(conversation.id, run.id)
    approval = (await approvals_of(kernel))[0]
    paused = await coordinator.pause(conversation.id, run.id)
    assert paused.status == "paused"
    await coordinator.decide(approval.id, "approve", approval.arguments_hash)
    await coordinator.decide(approval.id, "approve", approval.arguments_hash)
    assert not (work / "note.txt").exists()
    assert (await kernel.get_run(run.id)).status == "paused"
    await coordinator.resume(conversation.id, run.id)
    await coordinator.dispatch(conversation.id, run.id)
    assert (work / "note.txt").read_text(encoding="utf-8") == "later"
    await coordinator.finish(conversation.id, run.id)
    with pytest.raises(Conflict):
        await coordinator.resume(conversation.id, run.id)


async def test_pause_during_shell_waits_for_a_safe_point(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel)
    await propose(kernel, conversation.id, run.id, [
        ("run_shell", {"command": "sleep 0.8", "timeout": 5000}),
        ("run_shell", {"command": "echo x > marker.txt", "timeout": 5000}),
    ], "pause-shell")
    task = asyncio.create_task(coordinator.dispatch(conversation.id, run.id))
    saw_running = False
    for _ in range(40):
        await asyncio.sleep(0.05)
        if any(event.event_type == "action_started" for event in await kernel.list_events(conversation.id)) and not task.done():
            assert (await kernel.get_run(run.id)).status == "running"
            await coordinator.pause(conversation.id, run.id)
            saw_running = True
            break
    await asyncio.wait_for(task, timeout=5)
    assert saw_running
    assert (await kernel.get_run(run.id)).status == "paused"
    assert not (work / "marker.txt").exists()
    states = [action.state for action in sorted(await kernel.list_actions(conversation.id), key=lambda action: action.call_index)]
    assert states == ["finished", "proposed"]


async def test_cancel_rejects_actions_that_have_not_started(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel)
    await propose(kernel, conversation.id, run.id, [
        ("write_file", {"file_path": "a.txt", "content": "a"}),
        ("write_file", {"file_path": "b.txt", "content": "b"}),
    ], "cancel")
    cancelled = await coordinator.cancel(conversation.id, run.id)
    assert cancelled.status == "cancelled"
    await coordinator.dispatch(conversation.id, run.id)
    assert not (work / "a.txt").exists()
    assert not (work / "b.txt").exists()
    assert reasons(await kernel.list_events(conversation.id)) == ["cancelled", "cancelled"]


async def test_builtin_tools_report_structured_status(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel, attachment_dir=tmp_path / "attachments", output_limit=32)
    await propose(kernel, conversation.id, run.id, [
        ("write_file", {"file_path": "note.txt", "content": "alpha"}),
        ("read_file", {"file_path": "note.txt"}),
        ("edit_file", {"file_path": "note.txt", "old_string": "alpha", "new_string": "beta"}),
        ("grep_search", {"pattern": "beta", "include": "*.txt"}),
        ("list_files", {"pattern": "*.txt"}),
    ], "tools")
    await coordinator.dispatch(conversation.id, run.id)
    assert (work / "note.txt").read_text(encoding="utf-8") == "beta"
    events = await kernel.list_events(conversation.id)
    observations = [event.payload for event in events if event.event_type == "observation"]
    assert [item["status"] for item in observations] == ["success"] * 5
    assert "note.txt" in observations[3]["output"]
    assert "note.txt" in observations[4]["output"]
    assert await kernel.count_approvals() == 0

    long_run = run
    (work / "long.txt").write_text("x" * 80, encoding="utf-8")
    await propose(kernel, conversation.id, long_run.id, [("read_file", {"file_path": "long.txt"})], "long")
    await coordinator.dispatch(conversation.id, long_run.id)
    attached = next(
        event.payload for event in await kernel.list_events(conversation.id)
        if event.event_type == "observation" and event.payload.get("attachments")
    )
    assert attached["status"] == "success"
    assert "x" * 80 not in attached["output"]
    record = attached["attachments"][0]
    stored = (tmp_path / "attachments" / record["id"]).read_bytes()
    assert len(stored) == record["size"]
    assert __import__("hashlib").sha256(stored).hexdigest() == record["hash"]

    await propose(kernel, conversation.id, run.id, [(
        "run_shell",
        {"command": "echo success; exit 3"},
    )], "exit")
    await coordinator.dispatch(conversation.id, run.id)
    failed = next(
        event.payload for event in await kernel.list_events(conversation.id)
        if event.event_type == "observation" and (event.payload.get("error") or {}).get("code") == "exit"
    )
    assert failed["status"] == "error"
    assert failed["exit_code"] == 3
    assert "success" in failed["output"]
    probe = Event(
        event_id="e",
        conversation_id="c",
        seq=1,
        run_id=None,
        event_type="observation",
        schema_version=1,
        created_at="t",
        payload={"status": "error", "output": "success", "tool_call_id": "call"},
    )
    assert _terminal_message(probe, "anthropic")["is_error"] is True


async def test_accept_edits_confirms_only_ordinary_edits(kernel, tmp_path):
    edits = tmp_path / "edits"
    conversation, run = await open_run(kernel, edits, strategy="confirm_risky", body="edits")
    coordinator = Coordinator(kernel)
    coordinator.configure(conversation.id, PolicyConfig(accept_edits=True))
    await propose(kernel, conversation.id, run.id, [
        ("write_file", {"file_path": "note.txt", "content": "auto"}),
        ("run_shell", {"command": "echo ran > shell.txt"}),
    ], "accept")
    await coordinator.dispatch(conversation.id, run.id)
    assert (edits / "note.txt").read_text(encoding="utf-8") == "auto"
    assert not (edits / "shell.txt").exists()
    assert any(item.status == "pending" and item.conversation_id == conversation.id for item in await approvals_of(kernel))

    strict = tmp_path / "strict"
    other, other_run = await open_run(kernel, strict, strategy="always_confirm", body="strict")
    coordinator.configure(other.id, PolicyConfig(accept_edits=True))
    await propose(kernel, other.id, other_run.id, [("write_file", {"file_path": "note.txt", "content": "wait"})], "strict")
    await coordinator.dispatch(other.id, other_run.id)
    assert not (strict / "note.txt").exists()
    assert any(item.status == "pending" and item.conversation_id == other.id for item in await approvals_of(kernel))


def test_terminal_message_does_not_treat_success_text_as_status():
    event = SimpleNamespace(payload={"status": "success", "output": "error", "tool_call_id": "call"})
    assert "is_error" not in _terminal_message(event, "anthropic")
