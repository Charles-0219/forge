from __future__ import annotations

import hashlib
from pathlib import Path


class PathEscape(Exception):
    pass


def resolve_inside(cwd: Path | str, raw: str) -> Path:
    base = Path(cwd).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if resolved != base and base not in resolved.parents:
        raise PathEscape(str(resolved))
    return resolved


def file_digest(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
