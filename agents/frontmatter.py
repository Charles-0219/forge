"""Safe YAML frontmatter shared by memory, skills, and custom agents."""

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class FrontmatterResult:
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def parse_frontmatter(content: str) -> FrontmatterResult:
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=content)

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return FrontmatterResult(body=content)

    try:
        meta = yaml.safe_load("\n".join(lines[1:end_idx]))
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML frontmatter") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict) or any(not isinstance(key, str) for key in meta):
        raise ValueError("Frontmatter must be a mapping with string keys")

    body = "\n".join(lines[end_idx + 1:]).strip()
    return FrontmatterResult(meta=meta, body=body)


def text_field(meta: dict, key: str, default: str = "") -> str:
    value = meta.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def tool_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("allowed-tools must contain tool names")
    return [item.strip() for item in value]


def format_frontmatter(meta: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{header}\n---\n\n{body}"
