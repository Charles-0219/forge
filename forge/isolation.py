from __future__ import annotations

import hashlib
from pathlib import Path


class ScopeCache:
    """Caches keyed by cwd and config version. There is no process-global copy."""

    def __init__(self) -> None:
        self._skills: dict[tuple[str, str], dict[str, str]] = {}
        self._agents: dict[tuple[str, str], dict[str, str]] = {}
        self._permissions: dict[tuple[str, str], dict[str, list[str]]] = {}

    def put_skills(self, cwd: str, config_version: str, skills: dict[str, str]) -> None:
        self._skills[(cwd, config_version)] = dict(skills)

    def skills(self, cwd: str, config_version: str) -> dict[str, str]:
        return dict(self._skills.get((cwd, config_version), {}))

    def put_agents(self, cwd: str, config_version: str, agents: dict[str, str]) -> None:
        self._agents[(cwd, config_version)] = dict(agents)

    def agents(self, cwd: str, config_version: str) -> dict[str, str]:
        return dict(self._agents.get((cwd, config_version), {}))

    def put_permissions(self, cwd: str, config_version: str, rules: dict[str, list[str]]) -> None:
        self._permissions[(cwd, config_version)] = {key: list(value) for key, value in rules.items()}

    def permissions(self, cwd: str, config_version: str) -> dict[str, list[str]]:
        found = self._permissions.get((cwd, config_version), {})
        return {key: list(value) for key, value in found.items()}


def resolve_api_base(*, cli: str | None, api: str | None, env: dict[str, str]) -> str | None:
    for value in (cli, api, env.get("MINI_CLAUDE_API_BASE"), env.get("OPENAI_BASE_URL"), env.get("ANTHROPIC_BASE_URL")):
        if value:
            return value
    return None


def resolve_api_key(
    *,
    apikey: str | None,
    mini: str | None,
    protocol: str,
    openai_key: str | None,
    anthropic_key: str | None,
) -> str | None:
    if apikey:
        return apikey
    if mini:
        return mini
    if protocol == "openai":
        return openai_key or anthropic_key
    return anthropic_key or openai_key


def overlay_dotenv(process_env: dict[str, str], file_env: dict[str, str]) -> dict[str, str]:
    merged = dict(file_env)
    merged.update(process_env)
    return merged


def merge_named(user: dict[str, str], project: dict[str, str], *, project_wins: bool) -> dict[str, str]:
    if project_wins:
        merged = dict(user)
        merged.update(project)
        return merged
    merged = dict(project)
    merged.update(user)
    return merged


def merge_mcp(user_settings: dict, project_settings: dict, mcp_json: dict) -> dict:
    merged: dict = {}
    for source in (user_settings, project_settings, mcp_json):
        merged.update(source)
    return merged


def memory_root(project: Path) -> Path:
    digest = hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()[:16]
    return project / ".forge" / "memory" / digest


def write_memory(project: Path, name: str, body: str, *, permitted: bool) -> bool:
    if not permitted:
        return False
    path = memory_root(project) / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return True


def read_memory(project: Path) -> dict[str, str]:
    root = memory_root(project)
    if not root.is_dir():
        return {}
    return {path.stem: path.read_text(encoding="utf-8") for path in sorted(root.glob("*.md"))}
