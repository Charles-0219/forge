from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from forge.children import ChildRuntime
from forge.coordinator import Coordinator
from forge.errors import Conflict
from forge.isolation import (
    ScopeCache,
    merge_mcp,
    merge_named,
    overlay_dotenv,
    read_memory,
    resolve_api_base,
    resolve_api_key,
    write_memory,
)
from forge.kernel import Kernel
from forge.mcp import McpRegistry
from forge.memory_store import MemoryEventStore
from forge.policy import PolicyConfig
from forge.sqlite_store import SqliteEventStore
from tests.test_phase3_execution import approvals_of, open_run, propose, reasons


SECRET = "sk-live-sentinel"
ROOT = Path(__file__).resolve().parents[1]
FAKE_MCP = Path(__file__).resolve().parent / "fake_mcp_server.py"


@pytest.fixture(params=["memory", "sqlite"])
async def kernel(request, tmp_path):
    if request.param == "memory":
        yield Kernel(MemoryEventStore())
        return
    store = SqliteEventStore(tmp_path / "phase4.sqlite")
    await store.open()
    try:
        yield Kernel(store)
    finally:
        await store.close()


def _mcp_config(tmp_path, mode):
    return {
        "name": "fake",
        "command": sys.executable,
        "args": [str(FAKE_MCP)],
        "cwd": str(tmp_path),
        "env": {"FAKE_MCP_MODE": mode},
    }


async def test_child_inherits_config_and_keeps_credentials_in_memory(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel)
    children = ChildRuntime(coordinator)
    session = await children.spawn(
        conversation.id,
        run.id,
        kind="general",
        prompt="look around",
        trigger_action_id="action-1",
        credential_ref="cred-ref",
        thinking=False,
    )
    child = await kernel.get_conversation(session.conversation_id)
    child_run = await kernel.get_run(session.run_id)
    assert child.parent_id == conversation.id
    assert child.protocol == conversation.protocol
    assert child.model_ref == conversation.model_ref
    assert child.cwd == conversation.cwd
    assert child.confirm_strategy == conversation.confirm_strategy
    assert child_run.budget["thinking"] is False
    assert child_run.budget["root_run_id"] == run.id
    assert child_run.budget["trigger_action_id"] == "action-1"
    assert "max_calls" not in child_run.budget
    assert "max_tokens" not in child_run.budget
    assert children.credentials[child.id] == "cred-ref"
    blob = json.dumps({"events": [event.payload for event in await kernel.list_events(child.id)], "budget": child_run.budget})
    assert SECRET not in blob
    assert "cred-ref" not in blob


async def test_explore_plan_and_empty_allow_cannot_write_or_shell(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    coordinator = Coordinator(kernel)
    children = ChildRuntime(coordinator)
    explore = await children.spawn(conversation.id, run.id, kind="explore", prompt="explore", credential_ref="cred-ref")
    await propose(kernel, explore.conversation_id, explore.run_id, [
        ("write_file", {"file_path": "explore.txt", "content": "no"}),
        ("run_shell", {"command": "echo explore > marker.txt"}),
    ], "explore-tools")
    await coordinator.dispatch(explore.conversation_id, explore.run_id)
    await coordinator.finish(explore.conversation_id, explore.run_id)
    plan = await children.spawn(conversation.id, run.id, kind="plan", prompt="plan", credential_ref="cred-ref")
    await propose(kernel, plan.conversation_id, plan.run_id, [
        ("write_file", {"file_path": "plan.txt", "content": "no"}),
        ("run_shell", {"command": "echo plan > marker.txt"}),
    ], "plan-tools")
    await coordinator.dispatch(plan.conversation_id, plan.run_id)
    await coordinator.finish(plan.conversation_id, plan.run_id)
    empty = await children.spawn(
        conversation.id,
        run.id,
        kind="general",
        prompt="empty",
        child_allow=frozenset(),
        parent_allow=frozenset({"read_file", "write_file", "run_shell"}),
        credential_ref="cred-ref",
    )
    await propose(kernel, empty.conversation_id, empty.run_id, [
        ("read_file", {"file_path": "missing.txt"}),
    ], "empty-tools")
    await coordinator.dispatch(empty.conversation_id, empty.run_id)
    assert not (work / "explore.txt").exists()
    assert not (work / "plan.txt").exists()
    assert not (work / "marker.txt").exists()
    assert set(reasons(await kernel.list_events(explore.conversation_id))) == {"tool_not_allowed"}
    assert set(reasons(await kernel.list_events(plan.conversation_id))) <= {"tool_not_allowed", "plan_mode"}
    assert reasons(await kernel.list_events(empty.conversation_id)) == ["tool_not_allowed"]


async def test_parent_and_child_allowlists_intersect(kernel, tmp_path):
    work = tmp_path / "work"
    (work).mkdir()
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    (work / "note.txt").write_text("hi", encoding="utf-8")
    coordinator = Coordinator(kernel)
    children = ChildRuntime(coordinator)
    session = await children.spawn(
        conversation.id,
        run.id,
        kind="general",
        prompt="intersect",
        parent_allow=frozenset({"read_file"}),
        child_allow=frozenset({"read_file", "write_file"}),
        credential_ref="cred-ref",
    )
    await propose(kernel, session.conversation_id, session.run_id, [
        ("read_file", {"file_path": "note.txt"}),
        ("write_file", {"file_path": "note.txt", "content": "changed"}),
    ], "intersect")
    await coordinator.dispatch(session.conversation_id, session.run_id)
    events = await kernel.list_events(session.conversation_id)
    observations = [event.payload for event in events if event.event_type == "observation"]
    assert observations[0]["status"] == "success"
    assert "hi" in observations[0]["output"]
    assert reasons(events) == ["tool_not_allowed"]
    assert (work / "note.txt").read_text(encoding="utf-8") == "hi"


async def test_nested_and_overlapping_children_are_rejected(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work)
    coordinator = Coordinator(kernel)
    children = ChildRuntime(coordinator)
    first = await children.spawn(conversation.id, run.id, kind="general", prompt="one", credential_ref="cred-ref")
    with pytest.raises(Conflict) as busy:
        await children.spawn(conversation.id, run.id, kind="explore", prompt="two", credential_ref="cred-ref")
    assert busy.value.code == 409
    with pytest.raises(Conflict) as nested:
        await children.spawn(first.conversation_id, first.run_id, kind="skill_fork", prompt="again", credential_ref="cred-ref")
    assert nested.value.code == 409
    await coordinator.finish(first.conversation_id, first.run_id)
    second = await children.spawn(conversation.id, run.id, kind="skill_fork", prompt="after", credential_ref="cred-ref")
    assert second.kind == "skill_fork"
    with pytest.raises(Conflict):
        await children.spawn(second.conversation_id, second.run_id, kind="general", prompt="nested-skill", credential_ref="cred-ref")


async def test_descendant_usage_charges_the_root_budget(kernel, tmp_path):
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, budget={"max_calls": 1, "max_tokens": 10})
    children = ChildRuntime(Coordinator(kernel))
    session = await children.spawn(conversation.id, run.id, kind="general", prompt="budget", credential_ref="cred-ref")
    usage = await children.charge(session.conversation_id, calls=1, input_tokens=2, output_tokens=3)
    assert usage["calls"] == 1 and usage["tokens"] == 5 and usage["usage_unknown"] is False
    with pytest.raises(Conflict):
        await children.charge(session.conversation_id, calls=1, input_tokens=1, output_tokens=1)
    root = await kernel.get_run(run.id)
    assert root.usage == {"calls": 1, "tokens": 5, "usage_unknown": False}

    other, other_run = await open_run(
        kernel,
        tmp_path / "unknown",
        body="unknown-budget",
        budget={"max_calls": 4, "max_tokens": 1},
    )
    unknown = await children.spawn(other.id, other_run.id, kind="general", prompt="unknown", credential_ref="cred-ref")
    marked = await children.charge(unknown.conversation_id, calls=1, input_tokens=50, output_tokens=50, usage_unknown=True)
    assert marked["usage_unknown"] is True
    assert "tokens" not in marked
    assert (await kernel.get_run(other_run.id)).usage["usage_unknown"] is True


async def test_mcp_success_timeout_disconnect_protocol_and_close(kernel, tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_SECRET", SECRET)
    work = tmp_path / "work"
    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    registry = McpRegistry()
    registry.load([_mcp_config(tmp_path, "ok")])
    with pytest.raises(Conflict):
        registry.register_from_model({"name": "evil"})
    coordinator = Coordinator(kernel)
    coordinator.bind_mcp(registry)
    coordinator.configure(conversation.id, PolicyConfig(
        allow_tools=frozenset({"mcp__fake__echo"}),
        mcp_tools=frozenset({"mcp__fake__echo"}),
    ))
    await propose(kernel, conversation.id, run.id, [("mcp__fake__echo", {"text": "pong"})], "mcp-ok")
    started = time.monotonic()
    await coordinator.dispatch(conversation.id, run.id)
    assert time.monotonic() - started < 3
    observation = next(event.payload for event in await kernel.list_events(conversation.id) if event.event_type == "observation")
    assert observation["status"] == "success" and observation["output"] == "pong"
    session = registry.sessions["fake"]
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not any("mcp-stderr" in line for line in session.stderr):
        await asyncio.sleep(0.01)
    stderr = "".join(session.stderr)
    assert "mcp-stderr" in stderr
    assert "leaked-secret" not in stderr
    assert SECRET not in json.dumps(observation)
    await registry.close()

    async def failed(mode, response_id):
        failed_registry = McpRegistry()
        failed_registry.load([_mcp_config(tmp_path, mode)])
        failed_coordinator = Coordinator(kernel)
        failed_coordinator.bind_mcp(failed_registry)
        child, child_run = await open_run(kernel, work, strategy="never_confirm", body=response_id)
        failed_coordinator.configure(child.id, PolicyConfig(
            allow_tools=frozenset({"mcp__fake__echo"}),
            mcp_tools=frozenset({"mcp__fake__echo"}),
        ))
        arguments = {"text": "x", "timeout": 400} if mode == "timeout" else {"text": "x"}
        await propose(kernel, child.id, child_run.id, [("mcp__fake__echo", arguments)], response_id)
        begin = time.monotonic()
        await failed_coordinator.dispatch(child.id, child_run.id)
        elapsed = time.monotonic() - begin
        payload = next(event.payload for event in await kernel.list_events(child.id) if event.event_type == "observation")
        returncodes = [item.proc.returncode for item in failed_registry.sessions.values()]
        await failed_registry.close()
        return payload, elapsed, returncodes

    timeout_payload, elapsed, returncodes = await failed("timeout", "mcp-timeout")
    assert elapsed < 3
    assert timeout_payload["status"] == "error" and timeout_payload["error"]["code"] == "timeout"
    assert returncodes and all(code is not None for code in returncodes)
    disconnect_payload, _, _ = await failed("disconnect", "mcp-disconnect")
    assert disconnect_payload["error"]["code"] == "disconnect"
    protocol_payload, _, _ = await failed("bad", "mcp-protocol")
    assert protocol_payload["error"]["code"] == "protocol"

    hanging = McpRegistry()
    hanging.load([_mcp_config(tmp_path, "timeout")])
    from forge.dispatcher import Job

    job = Job("run", "conversation", "action", "call", "mcp__fake__echo", {"text": "wait", "timeout": 30000}, str(tmp_path), lambda: False)
    task = asyncio.create_task(hanging.call(job))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and "fake" not in hanging.sessions:
        await asyncio.sleep(0.01)
    await hanging.close()
    closed = await asyncio.wait_for(task, timeout=2)
    assert closed.status == "error"
    assert closed.error["code"] in {"timeout", "disconnect"}


async def test_mcp_allowlist_plan_mode_and_confirmation_gate_calls(kernel, tmp_path):
    work = tmp_path / "work"
    registry = McpRegistry()
    registry.load([_mcp_config(tmp_path, "ok")])
    await registry.connect("fake")
    assert registry.visible_tools(frozenset({"read_file"})) == []
    assert registry.visible_tools(frozenset({"mcp__fake__echo"})) == ["mcp__fake__echo"]
    await registry.close()

    conversation, run = await open_run(kernel, work, strategy="never_confirm")
    hidden = McpRegistry()
    hidden.load([_mcp_config(tmp_path, "ok")])
    coordinator = Coordinator(kernel)
    coordinator.bind_mcp(hidden)
    coordinator.configure(conversation.id, PolicyConfig(
        allow_tools=frozenset({"read_file"}),
        mcp_tools=frozenset({"mcp__fake__echo"}),
    ))
    await propose(kernel, conversation.id, run.id, [("mcp__fake__echo", {"text": "hidden"})], "mcp-hidden")
    await coordinator.dispatch(conversation.id, run.id)
    assert hidden.sessions == {}
    assert reasons(await kernel.list_events(conversation.id)) == ["tool_not_allowed"]

    planned, planned_run = await open_run(kernel, work, mode="plan", strategy="never_confirm", body="plan-mcp")
    planned_registry = McpRegistry()
    planned_registry.load([_mcp_config(tmp_path, "ok")])
    planned_coordinator = Coordinator(kernel)
    planned_coordinator.bind_mcp(planned_registry)
    planned_coordinator.configure(planned.id, PolicyConfig(
        allow_tools=frozenset({"mcp__fake__echo"}),
        mcp_tools=frozenset({"mcp__fake__echo"}),
    ))
    await propose(kernel, planned.id, planned_run.id, [("mcp__fake__echo", {"text": "no"})], "mcp-plan")
    await planned_coordinator.dispatch(planned.id, planned_run.id)
    assert planned_registry.sessions == {}
    assert reasons(await kernel.list_events(planned.id)) == ["plan_mode"]

    gated, gated_run = await open_run(kernel, work, strategy="confirm_risky", body="confirm-mcp")
    gated_registry = McpRegistry()
    gated_registry.load([_mcp_config(tmp_path, "ok")])
    gated_coordinator = Coordinator(kernel)
    gated_coordinator.bind_mcp(gated_registry)
    gated_coordinator.configure(gated.id, PolicyConfig(
        allow_tools=frozenset({"mcp__fake__echo"}),
        mcp_tools=frozenset({"mcp__fake__echo"}),
    ))
    await propose(kernel, gated.id, gated_run.id, [("mcp__fake__echo", {"text": "later"})], "mcp-confirm")
    await gated_coordinator.dispatch(gated.id, gated_run.id)
    assert gated_registry.sessions == {}
    approval = (await approvals_of(kernel))[-1]
    assert approval.status == "pending"
    await gated_coordinator.decide(approval.id, "approve", approval.arguments_hash)
    payload = next(event.payload for event in await kernel.list_events(gated.id) if event.event_type == "observation")
    assert payload["status"] == "success" and payload["output"] == "later"
    await gated_registry.close()


def test_scope_caches_and_memory_stay_inside_one_project(tmp_path):
    first = ScopeCache()
    second = ScopeCache()
    first.put_skills("/proj/a", "v1", {"search": "user"})
    first.put_agents("/proj/a", "v1", {"reviewer": "project"})
    first.put_permissions("/proj/a", "v1", {"allow": ["read_file"]})
    assert second.skills("/proj/a", "v1") == {}
    assert first.skills("/proj/b", "v1") == {}
    assert first.skills("/proj/a", "v2") == {}
    assert first.agents("/proj/a", "v1")["reviewer"] == "project"
    assert second.permissions("/proj/a", "v1") == {}

    left = tmp_path / "left"
    right = tmp_path / "right"
    assert write_memory(left, "notes", "left-only", permitted=False) is False
    assert read_memory(left) == {}
    assert write_memory(left, "notes", "left-only", permitted=True) is True
    assert write_memory(right, "notes", "right-only", permitted=True) is True
    assert read_memory(left) == {"notes": "left-only"}
    assert read_memory(right) == {"notes": "right-only"}
    written = list((left / ".forge").rglob("*.md"))
    assert written and written[0].is_relative_to(left)


def test_config_precedence_uses_the_supplied_values():
    env = {
        "MINI_CLAUDE_API_BASE": "https://mini.example",
        "OPENAI_BASE_URL": "https://openai.example",
        "ANTHROPIC_BASE_URL": "https://anthropic.example",
    }
    assert resolve_api_base(cli="https://cli.example", api="https://api.example", env=env) == "https://cli.example"
    assert resolve_api_base(cli=None, api="https://api.example", env=env) == "https://api.example"
    assert resolve_api_base(cli=None, api=None, env=env) == "https://mini.example"
    assert resolve_api_base(cli=None, api=None, env={"OPENAI_BASE_URL": "https://openai.example", "ANTHROPIC_BASE_URL": "https://anthropic.example"}) == "https://openai.example"
    assert resolve_api_base(cli=None, api=None, env={"ANTHROPIC_BASE_URL": "https://anthropic.example"}) == "https://anthropic.example"
    assert resolve_api_key(apikey="api-key", mini="mini-key", protocol="openai", openai_key="openai-key", anthropic_key="anthropic-key") == "api-key"
    assert resolve_api_key(apikey=None, mini="mini-key", protocol="anthropic", openai_key="openai-key", anthropic_key="anthropic-key") == "mini-key"
    assert resolve_api_key(apikey=None, mini=None, protocol="openai", openai_key=None, anthropic_key="anthropic-key") == "anthropic-key"
    assert resolve_api_key(apikey=None, mini=None, protocol="anthropic", openai_key="openai-key", anthropic_key=None) == "openai-key"
    assert overlay_dotenv({"APIKEY": "process"}, {"APIKEY": "file", "ONLY_FILE": "1"})["APIKEY"] == "process"
    assert merge_named({"shared": "user"}, {"shared": "project"}, project_wins=False)["shared"] == "user"
    assert merge_named({"shared": "user"}, {"shared": "project"}, project_wins=True)["shared"] == "project"
    merged = merge_mcp({"svc": {"from": "user"}}, {"svc": {"from": "project"}}, {"svc": {"from": "mcp.json"}})
    assert merged["svc"]["from"] == "mcp.json"


def test_new_runtime_has_one_effect_path():
    forge_dir = ROOT / "forge"
    offenders = []
    for path in forge_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "os.chdir" in text or ".chdir(" in text:
            offenders.append(f"chdir:{path.name}")
        if "subprocess" in text and path.name not in {"dispatcher.py", "mcp.py"}:
            offenders.append(f"subprocess:{path.name}")
        if "bypassPermissions" in text or "SKILL.md" in text:
            offenders.append(f"legacy:{path.name}")
    agent = (ROOT / "agents" / "agent.py").read_text(encoding="utf-8")
    assert 'else "bypassPermissions"' not in agent
    assert offenders == []
