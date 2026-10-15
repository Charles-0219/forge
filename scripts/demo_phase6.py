"""Reproducible phase-6 paths. They use a simulated model and do not open a network connection."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from forge.api import create_app
from forge.coordinator import Coordinator
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from forge.policy import PolicyConfig
from forge.skills import SkillService


API_KEY = "forge-demo-key"


def _response(text, calls=None, response_id="resp"):
    tool_calls = []
    for index, (name, arguments) in enumerate(calls or []):
        tool_calls.append({
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        })
    return {
        "id": response_id,
        "choices": [{"message": {"role": "assistant", "content": text, "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


async def run_demos(tmp_path: Path) -> dict:
    kernel = Kernel(MemoryEventStore())
    coordinator = Coordinator(kernel)
    skills = SkillService(kernel, scope=str(tmp_path))
    app = create_app(
        kernel,
        coordinator,
        skills,
        api_key=API_KEY,
        poll_interval=0.05,
        max_polls=0,
        allow_simulated_model=True,
    )
    headers = {"X-API-Key": API_KEY}
    work = tmp_path / "work"
    work.mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/conversations", headers=headers, json={
            "model_ref": "deepseek-flash",
            "protocol": "openai",
            "cwd": str(work),
            "confirm_strategy": "never_confirm",
        })
        conversation_id = created.json()["id"]
        coordinator.configure(conversation_id, PolicyConfig(deny_tools=frozenset({"write_file"})))
        rejected = await _turn(
            client,
            headers,
            conversation_id,
            "不要写文件",
            "reject-1",
            _response(None, [("write_file", {"file_path": "note.txt", "content": "no"})], "reject"),
        )
        first = await _turn(client, headers, conversation_id, "第一轮", "fold-1", _response("第一轮完成", response_id="fold-a"))
        second = await _turn(client, headers, conversation_id, "第二轮", "fold-2", _response("第二轮完成", response_id="fold-b"))
        await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"body": "还没做完", "client_request_id": "fold-3"},
        )
        folded = await client.post(f"/v1/conversations/{conversation_id}/compact", headers=headers)
        gated = await client.post("/v1/conversations", headers=headers, json={
            "model_ref": "deepseek-flash",
            "protocol": "openai",
            "cwd": str(work),
            "confirm_strategy": "always_confirm",
        })
        gated_id = gated.json()["id"]
        coordinator.configure(gated_id, PolicyConfig())
        waiting = await _turn(
            client,
            headers,
            gated_id,
            "需要批准",
            "approve-1",
            _response(None, [("write_file", {"file_path": "plan.txt", "content": "x"})], "approve"),
        )
        approval_id = waiting["pending_approvals"][0]["id"]
        arguments_hash = waiting["pending_approvals"][0]["arguments_hash"]
        await coordinator.recover()
        expired = await client.post(
            f"/v1/runs/{waiting['id']}/approvals/{approval_id}",
            headers=headers,
            json={"decision": "approve", "arguments_hash": arguments_hash},
        )
        return {
            "rejected_status": rejected["status"],
            "file_exists": (work / "note.txt").exists(),
            "folded": folded.json()["folded"],
            "turns": [first["id"], second["id"]],
            "expired_approval": expired.status_code,
        }


async def _turn(client, headers, conversation_id, body, request_id, response):
    message = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"body": body, "client_request_id": request_id},
    )
    message.raise_for_status()
    run_id = message.json()["id"]
    simulated = await client.post(
        f"/v1/conversations/{conversation_id}/runs/{run_id}/simulated-response",
        headers=headers,
        json={"response": response},
    )
    simulated.raise_for_status()
    return simulated.json()


if __name__ == "__main__":
    import asyncio
    import tempfile

    result = asyncio.run(run_demos(Path(tempfile.mkdtemp())))
    print(json.dumps(result, ensure_ascii=False))
