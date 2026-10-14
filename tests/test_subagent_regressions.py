from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents import agent as runtime, skills


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("entrypoint", ["agent", "skill"])
async def test_child_inherits_model_client_configuration(monkeypatch, agent_factory,
                                                       protocol, entrypoint):
    parent = agent_factory(protocol, thinking=False)
    created = []

    class FakeChild:
        def __init__(self, **kwargs):
            created.append(kwargs)

        async def run_once(self, prompt):
            return {"text": "child result", "tokens": {"input": 2, "output": 3}}

    monkeypatch.setattr(runtime, "Agent", FakeChild)
    monkeypatch.setattr(runtime, "get_sub_agent_config", lambda name: {
        "system_prompt": "Synthetic child", "tools": parent.tools,
    })
    monkeypatch.setattr(skills, "execute_skill", lambda *args: {
        "context": "fork", "prompt": "Synthetic skill", "allowed_tools": ["read_file"],
    })

    if entrypoint == "agent":
        result = await parent._execute_agent_tool({"type": "general", "prompt": "task"})
    else:
        result = await parent._execute_skill_tool({"skill_name": "synthetic", "args": "task"})

    assert result == "child result"
    assert created[0]["model"] == parent.model
    assert created[0]["api_key"] == "test-key"
    assert created[0]["thinking"] is parent.thinking
    if protocol == "openai":
        assert created[0]["api_base"] == str(parent._openai_client.base_url)
        assert created[0].get("anthropic_base_url") is None
    else:
        assert created[0]["anthropic_base_url"] == str(parent._anthropic_client.base_url)
        assert created[0].get("api_base") is None
    assert parent.get_token_usage() == {"input": 2, "output": 3}


def test_supported_thinking_configuration_initializes(agent_factory):
    parent = agent_factory("anthropic", model="claude-sonnet-4-6", thinking=True)
    assert parent._thinking_mode == "adaptive"
