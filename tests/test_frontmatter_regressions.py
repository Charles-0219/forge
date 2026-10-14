from __future__ import annotations

import pytest

from agents.frontmatter import format_frontmatter, parse_frontmatter
from agents.skills import _parse_skill_file
from agents.subagent import _load_agents_from_dir


def test_yaml_quotes_booleans_arrays_and_multiline():
    parsed = parse_frontmatter('''---
name: "synthetic"
description: |
  First line: with colon
  Second line
user-invocable: false
allowed-tools:
  - read_file
  - grep_search
---
Body
''')
    assert parsed.meta["name"] == "synthetic"
    assert parsed.meta["description"] == "First line: with colon\nSecond line\n"
    assert parsed.meta["user-invocable"] is False
    assert parsed.meta["allowed-tools"] == ["read_file", "grep_search"]
    assert parsed.body == "Body"


@pytest.mark.parametrize("raw", ["Plain markdown", "---\nname: x\nNo closing delimiter"])
def test_no_complete_frontmatter_preserves_body(raw):
    assert parse_frontmatter(raw).body == raw
    assert parse_frontmatter(raw).meta == {}


@pytest.mark.parametrize("raw", [
    "---\n- not-a-mapping\n---\nbody",
    "---\nname: [broken\n---\nbody",
    "---\nname: !!python/object/apply:os.system ['false']\n---\nbody",
])
def test_invalid_or_unsafe_yaml_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_frontmatter(raw)


def test_frontmatter_format_roundtrip():
    meta = {"name": "synthetic", "description": "Colon: value\nsecond line",
            "user-invocable": False, "allowed-tools": ["read_file", "grep_search"],
            "version": "0.1.0"}
    result = parse_frontmatter(format_frontmatter(meta, "Synthetic body"))
    assert result.meta == meta
    assert result.body == "Synthetic body"


@pytest.mark.parametrize("tools", ['[read_file, grep_search]', 'read_file, grep_search',
                                   '\n  - read_file\n  - grep_search'])
@pytest.mark.parametrize("flag", ["false", '"false"', "true", '"true"'])
def test_skill_yaml_types_and_legacy_forms(tmp_path, tools, flag):
    path = tmp_path / "SKILL.md"
    path.write_text(f'''---
name: synthetic
description: "Example: description"
user-invocable: {flag}
allowed-tools: {tools}
context: fork
---
Synthetic body
''', encoding="utf-8")
    skill = _parse_skill_file(path, "project", str(tmp_path))
    assert skill is not None
    assert skill.description == "Example: description"
    assert skill.user_invocable is ("true" in flag)
    assert skill.allowed_tools == ["read_file", "grep_search"]
    assert skill.context == "fork"


@pytest.mark.parametrize("field", ["name: [bad]", "description: true",
                                  "user-invocable: perhaps", "context: other",
                                  "allowed-tools: [read_file, 42]"])
def test_skill_invalid_field_types_are_not_loaded(tmp_path, field):
    path = tmp_path / "SKILL.md"
    path.write_text(f"---\n{field}\n---\nSynthetic body", encoding="utf-8")
    assert _parse_skill_file(path, "project", str(tmp_path)) is None


@pytest.mark.parametrize("tools", ['[read_file, grep_search]', 'read_file, grep_search',
                                   '\n  - read_file\n  - grep_search'])
def test_custom_agent_accepts_tool_lists(tmp_path, tools):
    (tmp_path / "synthetic.md").write_text(f'''---
name: "synthetic"
description: "Example: agent"
allowed-tools: {tools}
---
Synthetic prompt
''', encoding="utf-8")
    agents = {}
    _load_agents_from_dir(tmp_path, agents)
    assert agents["synthetic"]["allowed_tools"] == ["read_file", "grep_search"]


@pytest.mark.parametrize("tools", ["[read_file, 42]", "42", "{name: read_file}"])
def test_custom_agent_invalid_tools_do_not_broaden_permissions(tmp_path, tools):
    (tmp_path / "synthetic.md").write_text(
        f"---\nname: synthetic\nallowed-tools: {tools}\n---\nSynthetic prompt", encoding="utf-8")
    agents = {}
    _load_agents_from_dir(tmp_path, agents)
    assert "synthetic" not in agents
