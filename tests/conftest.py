from __future__ import annotations

import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setenv("BEAR_AUTO_SKILL_EVOLUTION", "0")
    for key in (
        "APIKEY", "API", "MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "MINI_CLAUDE_API_KEY",
        "MINI_CLAUDE_API_BASE",
    ):
        monkeypatch.delenv(key, raising=False)

    def forbid_network(*args, **kwargs):
        raise AssertionError("Regression tests must not connect to a network")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_network)

    from agents import session, skills, subagent, tools

    monkeypatch.setattr(session, "SESSION_DIR", home / ".bear-code" / "sessions")
    tools.reset_permission_cache()
    tools.reset_activated_tools()
    skills.reset_skill_cache()
    subagent.reset_agent_cache()
    yield {"home": home, "project": project}
    tools.reset_permission_cache()
    tools.reset_activated_tools()
    skills.reset_skill_cache()
    subagent.reset_agent_cache()


@pytest.fixture
def agent_factory(monkeypatch):
    from agents.agent import Agent

    def create(protocol="openai", **kwargs):
        options = {
            "model": "test-model",
            "api_key": "test-key",
            "custom_system_prompt": "Synthetic test prompt",
            "is_sub_agent": True,
        }
        if protocol == "openai":
            options["api_base"] = "https://example.invalid/v1"
        else:
            options["anthropic_base_url"] = "https://example.invalid/anthropic"
        options.update(kwargs)
        agent = Agent(**options)
        monkeypatch.setattr(agent, "_persist_large_result", lambda name, result: result)
        return agent

    return create
