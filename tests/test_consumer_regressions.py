from unittest.mock import AsyncMock

import pytest

from agents import agent as runtime, memory, skills, subagent


def test_custom_agent_empty_whitelist_stays_empty(isolated_runtime):
    directory = isolated_runtime["project"] / ".bear" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "empty.md").write_text(
        "---\nname: empty\nallowed-tools: []\n---\nSynthetic prompt", encoding="utf-8")
    assert subagent.get_sub_agent_config("empty")["tools"] == []


async def test_skill_fork_empty_whitelist_stays_empty(monkeypatch, agent_factory):
    parent = agent_factory()
    created = []

    class Child:
        def __init__(self, **kwargs):
            created.append(kwargs)

        run_once = AsyncMock(return_value={"text": "done", "tokens": {"input": 0, "output": 0}})

    monkeypatch.setattr(runtime, "Agent", Child)
    monkeypatch.setattr(skills, "execute_skill", lambda *args: {
        "context": "fork", "allowed_tools": [], "prompt": "Synthetic prompt",
    })
    await parent._execute_skill_tool({"skill_name": "empty"})
    assert created[0]["custom_tools"] == []


@pytest.mark.parametrize("field", ["name: [bad]", "description: true", "type: [project]"])
def test_memory_invalid_typed_metadata_is_not_loaded(field):
    directory = memory.get_memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bad.md").write_text(
        f"---\nname: synthetic\ntype: project\n{field}\n---\nSynthetic body", encoding="utf-8")
    assert memory.list_memories() == []


def test_memory_headers_reject_nontext_description():
    directory = memory.get_memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bad.md").write_text(
        "---\nname: synthetic\ntype: project\ndescription: [bad]\n---\nSynthetic body", encoding="utf-8")
    assert memory.scan_memory_headers() == []
