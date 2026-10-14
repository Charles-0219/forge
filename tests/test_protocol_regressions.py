from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import agent as runtime


def openai_response(names):
    calls = [
        {"id": f"call-{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps({"marker": i}),
        }}
        for i, name in enumerate(names)
    ]
    message = {"role": "assistant", "content": "done"}
    if calls:
        message.update(content=None, tool_calls=calls)
    return {"choices": [{"message": message}]}


def anthropic_response(names):
    blocks = [SimpleNamespace(type="tool_use", id=f"call-{i}", name=name,
                              input={"marker": i}) for i, name in enumerate(names)]
    if not blocks:
        blocks = [SimpleNamespace(type="text", text="done")]
    return SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def results_for(agent, protocol):
    if protocol == "openai":
        return [(m["tool_call_id"], m["content"]) for m in agent._openai_messages
                if m.get("role") == "tool"]
    return [(b["tool_use_id"], b["content"]) for m in agent._anthropic_messages
            if isinstance(m.get("content"), list) for b in m["content"]
            if b.get("type") == "tool_result"]


async def run_turn(agent, protocol, names):
    stream = AsyncMock(side_effect=[
        openai_response(names) if protocol == "openai" else anthropic_response(names),
        openai_response([]) if protocol == "openai" else anthropic_response([]),
    ])
    setattr(agent, f"_call_{protocol}_stream", stream)
    agent._check_and_compact = AsyncMock()
    await getattr(agent, f"_chat_{protocol}")("synthetic task")
    return stream


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("names", [
    ["read_file", "list_files"], ["write_file", "edit_file"],
    ["read_file", "write_file"], ["read_file", "list_files", "write_file"],
])
async def test_each_tool_call_executes_once(monkeypatch, agent_factory, protocol, names):
    agent = agent_factory(protocol)
    monkeypatch.setattr(runtime, "check_permission", lambda *args: {"action": "allow"})
    executed = []

    async def execute(name, arguments):
        executed.append(arguments["marker"])
        return f"result-{arguments['marker']}"

    agent._execute_tool_call = execute
    await run_turn(agent, protocol, names)
    assert sorted(executed) == list(range(len(names)))
    assert sorted(results_for(agent, protocol)) == [
        (f"call-{i}", f"result-{i}") for i in range(len(names))
    ]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("decisions", [
    ["deny"], ["deny", "deny"], ["allow", "deny"], ["deny", "allow"],
    ["confirm", "confirm"],
])
async def test_rejections_pair_every_call(monkeypatch, agent_factory, protocol, decisions):
    agent = agent_factory(protocol)
    permissions = iter(decisions)
    monkeypatch.setattr(runtime, "check_permission", lambda *args: {
        "action": next(permissions), "message": "Synthetic policy rejection",
    })
    agent._confirm_dangerous = AsyncMock(return_value=False)
    agent._execute_tool_call = AsyncMock(return_value="executed")
    await run_turn(agent, protocol, ["write_file"] * len(decisions))
    results = results_for(agent, protocol)
    assert [call_id for call_id, _ in results] == [f"call-{i}" for i in range(len(decisions))]
    assert agent._execute_tool_call.await_count == decisions.count("allow")
    for decision, (_, content) in zip(decisions, results):
        if decision != "allow":
            assert "denied" in content.lower()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_budget_stop_still_pairs_tool_calls(monkeypatch, agent_factory, protocol):
    agent = agent_factory(protocol)
    agent._check_budget = lambda: {"exceeded": True, "reason": "synthetic budget"}
    agent._execute_tool_call = AsyncMock()
    await run_turn(agent, protocol, ["read_file", "write_file"])
    assert [call_id for call_id, _ in results_for(agent, protocol)] == ["call-0", "call-1"]
    agent._execute_tool_call.assert_not_awaited()


async def test_anthropic_rejection_is_error(monkeypatch, agent_factory):
    agent = agent_factory("anthropic")
    monkeypatch.setattr(runtime, "check_permission", lambda *args: {
        "action": "deny", "message": "Synthetic deny",
    })
    await run_turn(agent, "anthropic", ["write_file"])
    blocks = [b for m in agent._anthropic_messages if isinstance(m.get("content"), list)
              for b in m["content"] if b.get("type") == "tool_result"]
    assert blocks[0]["is_error"] is True
