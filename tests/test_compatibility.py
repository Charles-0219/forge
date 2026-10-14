from __future__ import annotations

import json
from pathlib import Path

from agents import main, memory, skills, subagent, tools
from agents.mcp_client import McpManager


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_api_configuration_precedence(monkeypatch):
    monkeypatch.setenv("API", "https://generic.invalid/v1")
    monkeypatch.setenv("APIKEY", "synthetic-generic")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.invalid/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic")
    assert main._resolve_api_config(None) == ("https://generic.invalid/v1", "synthetic-generic", True)
    assert main._resolve_api_config("https://cli.invalid/anthropic") == (
        "https://cli.invalid/anthropic", "synthetic-generic", False)
    monkeypatch.delenv("APIKEY")
    assert main._resolve_api_config("https://cli.invalid/anthropic")[1] == "synthetic-anthropic"
    assert main._resolve_api_config("https://cli.invalid/v1")[1] == "synthetic-openai"


def test_dotenv_preserves_existing_environment(monkeypatch, isolated_runtime):
    project = isolated_runtime["project"]
    (project / ".env").write_text("MODEL=synthetic-file\nAPIKEY=synthetic-file-key\n", encoding="utf-8")
    monkeypatch.setenv("MODEL", "synthetic-shell")
    main._load_env_file()
    assert main.os.environ["MODEL"] == "synthetic-shell"
    assert main.os.environ["APIKEY"] == "synthetic-file-key"


def test_skill_user_scope_has_priority(isolated_runtime):
    for scope in ("home", "project"):
        path = isolated_runtime[scope] / ".bear" / "skills" / "same" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: same\ndescription: {scope}\n---\nSynthetic body", encoding="utf-8")
    discovered = skills.discover_skills()
    assert len(discovered) == 1
    assert discovered[0].description == "home"
    assert discovered[0].source == "user"


def test_agent_project_scope_has_priority(isolated_runtime):
    for scope in ("home", "project"):
        path = isolated_runtime[scope] / ".bear" / "agents" / "same.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: same\ndescription: {scope}\n---\nSynthetic prompt", encoding="utf-8")
    assert subagent._discover_custom_agents()["same"]["description"] == "project"


def test_permission_rules_merge_and_deny_wins(isolated_runtime):
    write_json(isolated_runtime["home"] / ".bear" / "settings.json", {
        "permissions": {"allow": ["read_file"], "deny": ["write_file"]}})
    write_json(isolated_runtime["project"] / ".bear" / "settings.json", {
        "permissions": {"allow": ["write_file"], "deny": ["run_shell"]}})
    rules = tools.load_permission_rules()
    assert len(rules["allow"]) == 2
    assert len(rules["deny"]) == 2
    assert tools.check_permission("write_file", {}, "default")["action"] == "deny"


def test_mcp_configuration_precedence(isolated_runtime):
    for scope in ("home", "project"):
        write_json(isolated_runtime[scope] / ".bear" / "settings.json", {
            "mcpServers": {"same": {"command": scope}, scope: {"command": scope}}})
    write_json(isolated_runtime["project"] / ".mcp.json", {
        "mcpServers": {"same": {"command": "mcp-json"}}})
    configs = McpManager()._load_configs()
    assert configs["same"]["command"] == "mcp-json"
    assert set(configs) == {"same", "home", "project"}


def test_memory_project_isolation_and_roundtrip(monkeypatch, isolated_runtime):
    first = memory.get_memory_dir()
    memory.save_memory("Synthetic name", "Description: synthetic", "project", "Synthetic content")
    entries = memory.list_memories()
    assert len(entries) == 1
    assert entries[0].description == "Description: synthetic"
    other = isolated_runtime["project"] / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert memory.get_memory_dir() != first
    assert memory.list_memories() == []


def test_empty_custom_tool_list_remains_empty(agent_factory):
    agent = agent_factory(custom_tools=[])
    assert agent.tools == []
