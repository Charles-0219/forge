from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("choice,expected_mode,cleared", [
    ("execute", "acceptEdits", False),
    ("manual-execute", "default", False),
    ("clear-and-execute", "acceptEdits", True),
    ("keep-planning", "plan", False),
])
async def test_plan_approval_awaits_body_and_changes_mode(agent_factory, tmp_path,
                                                       protocol, choice, expected_mode, cleared):
    agent = agent_factory(protocol)
    plan = tmp_path / "plan.md"
    plan.write_text("# Synthetic plan\n\nRun regression tests.\n", encoding="utf-8")
    agent.permission_mode = "plan"
    agent._pre_plan_mode = "default"
    agent._plan_file_path = str(plan)
    callback = AsyncMock(return_value={"choice": choice, "feedback": "revise"})
    agent.set_plan_approval_fn(callback)

    output = await agent._execute_plan_mode_tool("exit_plan_mode")

    callback.assert_awaited_once_with(plan.read_text(encoding="utf-8"))
    assert agent.permission_mode == expected_mode
    assert agent._context_cleared is cleared
    if choice == "keep-planning":
        assert agent._plan_file_path == str(plan)
    else:
        assert agent._plan_file_path is None
        assert agent._pre_plan_mode is None
        assert "# Synthetic plan" in output
        if protocol == "openai":
            assert agent._openai_messages[0]["content"] == agent._base_system_prompt


async def test_unrecognized_approval_does_not_authorize_execution(agent_factory, tmp_path):
    agent = agent_factory()
    plan = tmp_path / "plan.md"
    plan.write_text("Synthetic plan", encoding="utf-8")
    agent.permission_mode = "plan"
    agent._plan_file_path = str(plan)
    agent.set_plan_approval_fn(AsyncMock(return_value={"choice": "invalid"}))
    await agent._execute_plan_mode_tool("exit_plan_mode")
    assert agent.permission_mode == "plan"


async def test_missing_plan_stays_in_plan_mode(agent_factory):
    agent = agent_factory()
    agent.permission_mode = "plan"
    callback = AsyncMock(return_value={"choice": "execute"})
    agent.set_plan_approval_fn(callback)
    await agent._execute_plan_mode_tool("exit_plan_mode")
    assert agent.permission_mode == "plan"
    callback.assert_not_awaited()


async def test_plan_without_approval_callback_stays_in_plan_mode(agent_factory, tmp_path):
    agent = agent_factory()
    plan = tmp_path / "plan.md"
    plan.write_text("Synthetic plan", encoding="utf-8")
    agent.permission_mode = "plan"
    agent._plan_file_path = str(plan)
    await agent._execute_plan_mode_tool("exit_plan_mode")
    assert agent.permission_mode == "plan"
