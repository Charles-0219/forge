from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine, inspect

from forge.errors import Conflict, DuplicateTerminal, IncompletePairing, NotFound
from forge.events import EVENT_TYPES
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from forge.migrate import upgrade
from forge.schema import Base
from forge.sqlite_store import SqliteEventStore


@pytest.fixture(params=["memory", "sqlite"])
async def kernel(request, tmp_path):
    if request.param == "memory":
        yield Kernel(MemoryEventStore())
        return
    store = SqliteEventStore(tmp_path / "phase2.sqlite")
    await store.open()
    try:
        yield Kernel(store)
    finally:
        await store.close()


def openai_turn(names, call_id="resp-openai"):
    calls = [
        {"id": f"call-{index}", "type": "function", "function": {
            "name": name, "arguments": json.dumps({"marker": index}),
        }}
        for index, name in enumerate(names)
    ]
    message = {"role": "assistant", "content": "done"}
    if calls:
        message.update(content=None, tool_calls=calls)
    return {"id": call_id, "choices": [{"message": message}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


def anthropic_turn(names, call_id="resp-anthropic"):
    blocks = [
        SimpleNamespace(type="tool_use", id=f"call-{index}", name=name, input={"marker": index})
        for index, name in enumerate(names)
    ]
    if not blocks:
        blocks = [SimpleNamespace(type="text", text="done")]
    return SimpleNamespace(
        id=call_id,
        content=blocks,
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )


async def open_conversation(kernel, protocol="openai"):
    conversation = await kernel.create_conversation(
        protocol=protocol,
        model_ref="deepseek-flash",
        cwd="/synthetic/project",
        mode="default",
        confirm_strategy="confirm_risky",
    )
    run = await kernel.submit_message(conversation.id, body="synthetic task", client_request_id="req-1")
    return conversation, run


def tool_messages(projection):
    if projection.protocol == "openai":
        return [message for message in projection.messages if message.get("role") == "tool"]
    return [
        block
        for message in projection.messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def scrub(value, ids):
    if isinstance(value, dict):
        return {
            key: scrub(item, ids)
            for key, item in value.items()
            if key not in {"created_at", "event_id"}
        }
    if isinstance(value, list):
        return [scrub(item, ids) for item in value]
    if isinstance(value, str):
        try:
            UUID(value)
        except ValueError:
            return value
        return ids.setdefault(value, f"id{len(ids)}")
    return value


async def scripted(kernel, protocol):
    conversation, run = await open_conversation(kernel, protocol)
    await kernel.prepare_context(
        conversation.id,
        system_prompt="stable system",
        memories=[{"id": "m1", "content": "alpha"}],
        model={"name": "deepseek-flash", "api_key": "sk-synthetic"},
    )
    response = openai_turn(["read_file", "list_files"]) if protocol == "openai" else anthropic_turn(["read_file", "list_files"])
    turn = await kernel.record_response(conversation.id, run.id, response)
    for action_index in range(2):
        action_id = next(
            event.payload["action_id"]
            for event in await kernel.list_events(conversation.id)
            if event.event_type == "action" and event.payload["call_index"] == action_index
        )
        await kernel.settle_action(conversation.id, action_id, kind="finished", output=f"result-{action_index}")
    prepared = await kernel.prepare_context(conversation.id, system_prompt="stable system")
    projection = await kernel.project(conversation.id, prepared.event_id)
    events = await kernel.list_events(conversation.id)
    return {
        "types": [event.event_type for event in events],
        "payloads": [scrub(event.payload, {}) for event in events],
        "hash": projection.input_hash,
        "results": [(item.get("tool_call_id") or item.get("tool_use_id"), item["content"]) for item in tool_messages(projection)],
        "call": turn.model_call_id,
    }


async def test_backends_record_the_same_turn(tmp_path):
    memory = Kernel(MemoryEventStore())
    sqlite = SqliteEventStore(tmp_path / "match.sqlite")
    await sqlite.open()
    try:
        left = await scripted(memory, "openai")
        right = await scripted(Kernel(sqlite), "openai")
    finally:
        await sqlite.close()
    assert left["types"] == right["types"]
    assert left["payloads"] == right["payloads"]
    assert left["results"] == right["results"]


async def test_two_tools_have_one_terminal_each(kernel):
    conversation, run = await open_conversation(kernel, "openai")
    await kernel.record_response(conversation.id, run.id, openai_turn(["read_file", "write_file"]))
    events = await kernel.list_events(conversation.id)
    action_ids = [event.payload["action_id"] for event in events if event.event_type == "action"]
    await kernel.settle_action(conversation.id, action_ids[0], kind="finished", output="one")
    with pytest.raises(IncompletePairing):
        await kernel.prepare_context(conversation.id, system_prompt="stable system")
    await kernel.settle_action(conversation.id, action_ids[1], kind="finished", output="two")
    prepared = await kernel.prepare_context(conversation.id, system_prompt="stable system")
    projection = await kernel.project(conversation.id, prepared.event_id)
    assert sorted(tool_messages(projection), key=lambda item: item["tool_call_id"]) == [
        {"role": "tool", "tool_call_id": "call-0", "content": "one"},
        {"role": "tool", "tool_call_id": "call-1", "content": "two"},
    ]
    assert not any(event.event_type == "action_started" for event in await kernel.list_events(conversation.id))


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_rejections_and_unknown_results_pair(kernel, protocol):
    conversation, run = await open_conversation(kernel, protocol)
    response = openai_turn(["read_file", "write_file"]) if protocol == "openai" else anthropic_turn(["read_file", "write_file"])
    await kernel.record_response(conversation.id, run.id, response)
    actions = [event for event in await kernel.list_events(conversation.id) if event.event_type == "action"]
    await kernel.settle_action(conversation.id, actions[0].payload["action_id"], kind="rejected", reason="denied")
    await kernel.settle_action(conversation.id, actions[1].payload["action_id"], kind="outcome_unknown")
    prepared = await kernel.prepare_context(conversation.id, system_prompt="stable system")
    projection = await kernel.project(conversation.id, prepared.event_id)
    results = tool_messages(projection)
    assert [item.get("tool_call_id") or item.get("tool_use_id") for item in results] == ["call-0", "call-1"]
    assert results[0]["content"].startswith("Not executed: denied")
    assert "could not be confirmed" in results[1]["content"]
    assert "success" not in results[1]["content"].lower()
    if protocol == "anthropic":
        assert results[0]["is_error"] is True
        assert results[1]["is_error"] is True


async def test_budget_skip_sets_anthropic_error(kernel):
    conversation, run = await open_conversation(kernel, "anthropic")
    await kernel.record_response(conversation.id, run.id, anthropic_turn(["read_file", "list_files"]))
    for event in await kernel.list_events(conversation.id):
        if event.event_type == "action":
            await kernel.settle_action(
                conversation.id,
                event.payload["action_id"],
                kind="rejected",
                reason="budget_exceeded",
            )
    prepared = await kernel.prepare_context(conversation.id, system_prompt="stable system")
    results = tool_messages(await kernel.project(conversation.id, prepared.event_id))
    assert len(results) == 2
    assert all(item["is_error"] is True and "budget_exceeded" in item["content"] for item in results)


async def test_unsupported_openai_call_is_paired_without_execution(kernel):
    conversation, run = await open_conversation(kernel, "openai")
    response = openai_turn(["read_file"])
    response["choices"][0]["message"]["tool_calls"].append({"id": "call-x", "type": "custom"})
    await kernel.record_response(conversation.id, run.id, response)
    events = await kernel.list_events(conversation.id)
    normal = next(event for event in events if event.event_type == "action" and event.payload["tool_call_id"] == "call-0")
    with pytest.raises(IncompletePairing):
        await kernel.prepare_context(conversation.id, system_prompt="stable system")
    await kernel.settle_action(conversation.id, normal.payload["action_id"], kind="finished", output="ok")
    rejected = next(event for event in await kernel.list_events(conversation.id) if event.event_type == "action_rejected")
    assert rejected.payload["reason"] == "unsupported_tool_call"
    prepared = await kernel.prepare_context(conversation.id, system_prompt="stable system")
    results = {item["tool_call_id"]: item["content"] for item in tool_messages(await kernel.project(conversation.id, prepared.event_id))}
    assert results == {"call-0": "ok", "call-x": "Not executed: unsupported_tool_call"}


async def test_duplicate_terminal_is_rejected(kernel):
    conversation, run = await open_conversation(kernel)
    await kernel.record_response(conversation.id, run.id, openai_turn(["read_file"]))
    action_id = next(event.payload["action_id"] for event in await kernel.list_events(conversation.id) if event.event_type == "action")
    await kernel.settle_action(conversation.id, action_id, kind="finished", output="ok")
    with pytest.raises(DuplicateTerminal):
        await kernel.settle_action(conversation.id, action_id, kind="rejected", reason="again")


async def test_recorded_context_ignores_later_disk_changes(kernel, tmp_path):
    conversation, _run = await open_conversation(kernel)
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("alpha", encoding="utf-8")
    prepared = await kernel.prepare_context(
        conversation.id,
        system_prompt="stable system",
        memories=[{"id": "m1", "content": "alpha"}],
        skills=[{"version_id": "v1", "content": skill_file.read_text(encoding="utf-8")}],
        model={"name": "deepseek-flash", "api_key": "sk-synthetic"},
    )
    skill_file.write_text("beta", encoding="utf-8")
    projection = await kernel.project(conversation.id, prepared.event_id)
    assert projection.input_hash == prepared.payload["input_hash"]
    rendered = json.dumps(projection.system, ensure_ascii=False)
    assert "alpha" in rendered
    assert "beta" not in rendered
    assert "sk-synthetic" not in json.dumps(prepared.payload)


async def test_same_inputs_keep_a_stable_system_prompt(kernel):
    first, _run = await open_conversation(kernel)
    first_context = await kernel.prepare_context(first.id, system_prompt="stable system")
    second = await kernel.create_conversation(
        protocol="openai",
        model_ref="deepseek-flash",
        cwd="/synthetic/project",
        mode="default",
        confirm_strategy="confirm_risky",
    )
    await kernel.submit_message(second.id, body="synthetic task", client_request_id="req-1")
    second_context = await kernel.prepare_context(second.id, system_prompt="stable system")
    assert first_context.payload["system_prompt"] == "stable system"
    assert first_context.payload["input_hash"] == second_context.payload["input_hash"]


async def test_request_id_is_idempotent_and_conflicts(kernel):
    conversation, run = await open_conversation(kernel)
    again = await kernel.submit_message(conversation.id, body="synthetic task", client_request_id="req-1")
    assert again.id == run.id
    assert [event.event_type for event in await kernel.list_events(conversation.id)].count("user_message") == 1
    with pytest.raises(Conflict):
        await kernel.submit_message(conversation.id, body="different task", client_request_id="req-1")
    with pytest.raises(Conflict):
        await kernel.submit_message(conversation.id, body="another", client_request_id="req-2")
    assert [event.event_type for event in await kernel.list_events(conversation.id)].count("user_message") == 1


async def test_failed_submit_does_not_leave_a_message(kernel):
    conversation = await kernel.create_conversation(
        protocol="openai",
        model_ref="deepseek-flash",
        cwd="/synthetic/project",
        mode="default",
        confirm_strategy="confirm_risky",
    )
    kernel.store.fail_after_writes = 1
    with pytest.raises(RuntimeError, match="injected write failure"):
        await kernel.submit_message(conversation.id, body="synthetic task", client_request_id="req-1")
    kernel.store.fail_after_writes = None
    await kernel.submit_message(conversation.id, body="synthetic task", client_request_id="req-1")
    assert [event.event_type for event in await kernel.list_events(conversation.id)].count("user_message") == 1


async def test_failed_model_record_does_not_leave_actions(kernel):
    conversation, run = await open_conversation(kernel)
    before = len(await kernel.list_events(conversation.id))
    kernel.store.fail_after_writes = 1
    with pytest.raises(RuntimeError, match="injected write failure"):
        await kernel.record_response(conversation.id, run.id, openai_turn(["read_file", "list_files"]))
    kernel.store.fail_after_writes = None
    assert len(await kernel.list_events(conversation.id)) == before
    with pytest.raises(NotFound):
        await kernel.settle_action(conversation.id, "missing", kind="finished", output="nope")


async def test_approvals_table_is_unused_and_migration_matches_metadata(tmp_path):
    path = tmp_path / "migrated.sqlite"
    upgrade(path)
    engine = create_engine("sqlite:///" + path.resolve().as_posix())
    names = set(inspect(engine).get_table_names())
    assert {"conversations", "events", "runs", "actions", "approvals"} <= names
    for table_name, table in Base.metadata.tables.items():
        columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
        assert columns == set(table.columns.keys())
    engine.dispose()
    assert "approval_decided" in EVENT_TYPES
    assert "skill_promoted" in EVENT_TYPES


async def test_phase2_catalog_does_not_execute_tools(kernel):
    conversation, run = await open_conversation(kernel)
    await kernel.record_response(conversation.id, run.id, openai_turn(["run_shell"]))
    events = await kernel.list_events(conversation.id)
    assert [event.event_type for event in events if event.event_type == "action"] == ["action"]
    assert await kernel.count_approvals() == 0
