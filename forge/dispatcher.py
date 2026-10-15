from __future__ import annotations

import asyncio
import hashlib
import os
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from forge.paths import PathEscape, file_digest, resolve_inside


@dataclass
class Job:
    run_id: str
    conversation_id: str
    action_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict
    cwd: str
    cancelled: Callable[[], bool]


@dataclass
class ToolResult:
    status: str
    output: str
    exit_code: int | None = None
    error: dict | None = None
    attachments: list[dict] = field(default_factory=list)
    content_hash: str | None = None
    canonical_path: str | None = None


class Dispatcher:
    """Execute the six built-in tools. Success and failure come from the result status."""

    def __init__(
        self,
        *,
        processes: dict[str, asyncio.subprocess.Process],
        attachments: dict[str, dict],
        attachment_dir: Path | None = None,
        output_limit: int = 8192,
    ) -> None:
        self.processes = processes
        self.attachments = attachments
        self.attachment_dir = attachment_dir
        self.output_limit = output_limit

    async def run(self, job: Job) -> ToolResult:
        if job.tool_name == "run_shell":
            result = await self._shell(job)
        else:
            result = await asyncio.to_thread(self._sync, job)
        output, attachments = self._store_large(result.output)
        result.output = output
        result.attachments = attachments
        return result

    def _sync(self, job: Job) -> ToolResult:
        try:
            if job.tool_name == "read_file":
                return self._read(job)
            if job.tool_name == "write_file":
                return self._write(job)
            if job.tool_name == "edit_file":
                return self._edit(job)
            if job.tool_name == "list_files":
                return self._list(job)
            if job.tool_name == "grep_search":
                return self._grep(job)
        except PathEscape as exc:
            return ToolResult("error", "", error={"code": "symlink_escape", "path": str(exc)})
        return ToolResult("error", "", error={"code": "unsupported_tool"})

    def _read(self, job: Job) -> ToolResult:
        path = resolve_inside(job.cwd, str(job.arguments["file_path"]))
        if not path.is_file():
            return ToolResult("error", "", error={"code": "not_found", "path": str(path)})
        text = path.read_text(encoding="utf-8", errors="replace")
        numbered = "\n".join(f"{index:4d} | {line}" for index, line in enumerate(text.split("\n"), 1))
        return ToolResult("success", numbered, content_hash=file_digest(path), canonical_path=str(path))

    def _write(self, job: Job) -> ToolResult:
        path = resolve_inside(job.cwd, str(job.arguments["file_path"]))
        if path.exists() and not path.is_file():
            return ToolResult("error", "", error={"code": "not_a_file", "path": str(path)})
        path.write_text(str(job.arguments["content"]), encoding="utf-8")
        return ToolResult("success", "", content_hash=file_digest(path), canonical_path=str(path))

    def _edit(self, job: Job) -> ToolResult:
        path = resolve_inside(job.cwd, str(job.arguments["file_path"]))
        if not path.is_file():
            return ToolResult("error", "", error={"code": "not_found", "path": str(path)})
        text = path.read_text(encoding="utf-8", errors="replace")
        old = str(job.arguments["old_string"])
        count = text.count(old)
        if count != 1:
            return ToolResult("error", "", error={"code": "match_count", "count": count}, canonical_path=str(path))
        path.write_text(text.replace(old, str(job.arguments["new_string"]), 1), encoding="utf-8")
        return ToolResult("success", "", content_hash=file_digest(path), canonical_path=str(path))

    def _list(self, job: Job) -> ToolResult:
        base = resolve_inside(job.cwd, str(job.arguments.get("path") or "."))
        cwd = Path(job.cwd).resolve()
        matches = []
        for path in base.glob(str(job.arguments["pattern"])):
            resolved = path.resolve()
            if resolved == cwd or cwd in resolved.parents:
                matches.append(str(resolved.relative_to(cwd)))
        return ToolResult("success", "\n".join(sorted(matches)))

    def _grep(self, job: Job) -> ToolResult:
        base = resolve_inside(job.cwd, str(job.arguments.get("path") or "."))
        cwd = Path(job.cwd).resolve()
        include = job.arguments.get("include")
        try:
            pattern = re.compile(str(job.arguments["pattern"]))
        except re.error as exc:
            return ToolResult("error", "", error={"code": "invalid_pattern", "message": str(exc)})
        files = [base] if base.is_file() else [path for path in base.rglob("*") if path.is_file()]
        lines = []
        for path in files:
            resolved = path.resolve()
            if resolved != cwd and cwd not in resolved.parents:
                continue
            if include and not resolved.match(str(include)):
                continue
            text = resolved.read_text(encoding="utf-8", errors="replace")
            relative = str(resolved.relative_to(cwd))
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    lines.append(f"{relative}:{number}:{line}")
        return ToolResult("success", "\n".join(lines))

    async def _shell(self, job: Job) -> ToolResult:
        timeout_s = float(job.arguments.get("timeout", 30000)) / 1000.0
        try:
            proc = await asyncio.create_subprocess_shell(
                str(job.arguments["command"]),
                cwd=job.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return ToolResult("error", "", error={"code": "spawn_failed", "message": str(exc)})
        self.processes[job.run_id] = proc
        try:
            stdout, stderr, reason = await self._communicate(proc, timeout_s, job.cancelled)
        finally:
            self.processes.pop(job.run_id, None)
        output = _decode(stdout)
        err = _decode(stderr)
        if err:
            output = f"{output}\n{err}" if output else err
        if reason == "timeout":
            return ToolResult("error", output, exit_code=proc.returncode, error={"code": "timeout"})
        if reason == "cancelled":
            if proc.returncode is None:
                return ToolResult(
                    "outcome_unknown",
                    "Result unknown: the tool outcome could not be confirmed.",
                    error={"code": "cancelled"},
                )
            return ToolResult("error", output, exit_code=proc.returncode, error={"code": "cancelled"})
        if proc.returncode == 0:
            return ToolResult("success", output, exit_code=0)
        return ToolResult("error", output, exit_code=proc.returncode, error={"code": "exit"})

    async def _communicate(self, proc, timeout_s: float, cancelled: Callable[[], bool]):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        comm = asyncio.create_task(proc.communicate())
        reason = None
        while not comm.done():
            if cancelled():
                reason = "cancelled"
                _kill_group(proc)
                break
            if loop.time() >= deadline:
                reason = "timeout"
                _kill_group(proc)
                break
            await asyncio.sleep(0.02)
        try:
            stdout, stderr = await comm
        except Exception:
            stdout, stderr = b"", b""
        return stdout, stderr, reason

    def _store_large(self, output: str) -> tuple[str, list[dict]]:
        encoded = output.encode("utf-8")
        if len(encoded) <= self.output_limit:
            return output, []
        import uuid

        attachment_id = str(uuid.uuid4())
        digest = hashlib.sha256(encoded).hexdigest()
        record = {"id": attachment_id, "size": len(encoded), "hash": digest}
        self.attachments[attachment_id] = {**record, "bytes": encoded}
        if self.attachment_dir is not None:
            self.attachment_dir.mkdir(parents=True, exist_ok=True)
            (self.attachment_dir / attachment_id).write_bytes(encoded)
        return f"[attachment {attachment_id} {len(encoded)} bytes]", [record]


def _decode(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", errors="replace")


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None or proc.pid is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
