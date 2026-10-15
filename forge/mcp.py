from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

from forge.dispatcher import ToolResult, _kill_group
from forge.errors import Conflict


class McpFailure(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


@dataclass
class McpService:
    name: str
    command: str
    args: list[str]
    cwd: str
    env: dict[str, str]


@dataclass
class _Session:
    service: McpService
    proc: asyncio.subprocess.Process
    tools: list[dict] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    stderr_task: asyncio.Task | None = None
    next_id: int = 1


class McpRegistry:
    """User-configured stdio MCP sessions. The model cannot add services."""

    def __init__(self) -> None:
        self.services: dict[str, McpService] = {}
        self.sessions: dict[str, _Session] = {}

    def load(self, configs: list[dict]) -> None:
        self.services = {}
        for item in configs:
            name = str(item["name"])
            self.services[name] = McpService(
                name=name,
                command=str(item["command"]),
                args=[str(arg) for arg in item.get("args") or []],
                cwd=str(item["cwd"]),
                env={str(key): str(value) for key, value in (item.get("env") or {}).items()},
            )

    def register_from_model(self, config: dict) -> None:
        raise Conflict("the model cannot register an MCP service")

    def visible_tools(self, allow: frozenset[str] | None) -> list[str]:
        names = []
        for session in self.sessions.values():
            for tool in session.tools:
                exposed = f"mcp__{session.service.name}__{tool['name']}"
                if allow is not None and exposed not in allow:
                    continue
                names.append(exposed)
        return names

    async def connect(self, name: str) -> _Session:
        if name in self.sessions:
            return self.sessions[name]
        service = self.services.get(name)
        if service is None:
            raise McpFailure("unknown_service", name)
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(service.env)
        proc = await asyncio.create_subprocess_exec(
            service.command,
            *service.args,
            cwd=service.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        session = _Session(service=service, proc=proc)
        session.stderr_task = asyncio.create_task(self._drain_stderr(session))
        self.sessions[name] = session
        try:
            await self._request(session, "initialize", {}, 5)
            listed = await self._request(session, "tools/list", {}, 5)
        except McpFailure:
            await self.close()
            raise
        session.tools = list((listed or {}).get("tools") or [])
        return session

    async def call(self, job) -> ToolResult:
        parts = job.tool_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return ToolResult("error", "", error={"code": "protocol", "message": "bad mcp name"})
        timeout_s = float(job.arguments.get("timeout", 30000)) / 1000.0
        try:
            session = await self.connect(parts[1])
            result = await self._request(
                session,
                "tools/call",
                {"name": parts[2], "arguments": {key: value for key, value in job.arguments.items() if key != "timeout"}},
                timeout_s,
            )
        except McpFailure as exc:
            return ToolResult("error", "", error={"code": exc.code, "message": exc.message})
        text = ""
        for block in (result or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text += str(block.get("text") or "")
        return ToolResult("success", text)

    async def close(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            if session.proc.returncode is None:
                _kill_group(session.proc)
            if session.stderr_task is not None:
                session.stderr_task.cancel()
            if session.proc.stdin is not None:
                session.proc.stdin.close()
            try:
                await asyncio.wait_for(session.proc.wait(), timeout=2)
            except TimeoutError:
                session.proc.kill()
                await session.proc.wait()

    async def _request(self, session: _Session, method: str, params: dict, timeout_s: float) -> dict:
        if session.proc.returncode is not None or session.proc.stdin is None or session.proc.stdout is None:
            raise McpFailure("disconnect")
        request_id = session.next_id
        session.next_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
        session.proc.stdin.write(payload.encode("utf-8"))
        await session.proc.stdin.drain()
        try:
            line = await asyncio.wait_for(session.proc.stdout.readline(), timeout_s)
        except TimeoutError as exc:
            _kill_group(session.proc)
            try:
                await asyncio.wait_for(session.proc.wait(), timeout=2)
            except TimeoutError:
                session.proc.kill()
                await session.proc.wait()
            raise McpFailure("timeout") from exc
        if not line:
            raise McpFailure("disconnect")
        try:
            message = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise McpFailure("protocol") from exc
        if not isinstance(message, dict) or message.get("id") != request_id or "error" in message:
            raise McpFailure("protocol")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpFailure("protocol")
        return result

    async def _drain_stderr(self, session: _Session) -> None:
        if session.proc.stderr is None:
            return
        try:
            while True:
                line = await session.proc.stderr.readline()
                if not line:
                    return
                session.stderr.append(line.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            return
