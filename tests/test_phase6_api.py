from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from forge.api import create_app
from forge.cli import main
from forge.coordinator import Coordinator
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from forge.policy import PolicyConfig
from forge.skills import SkillService
from forge.sqlite_store import SqliteEventStore
from scripts.demo_phase6 import run_demos


ROOT = Path(__file__).resolve().parents[1]
KEY = "test-api-key"


@pytest.fixture(params=["memory", "sqlite"])
async def client(request, tmp_path):
    if request.param == "memory":
        kernel = Kernel(MemoryEventStore())
        closer = None
    else:
        store = SqliteEventStore(tmp_path / "phase6.sqlite")
        await store.open()
        kernel = Kernel(store)
        closer = store.close
    coordinator = Coordinator(kernel)
    skills = SkillService(kernel, scope=str(tmp_path / "scope"))
    app = create_app(
        kernel,
        coordinator,
        skills,
        api_key=KEY,
        poll_interval=0.05,
        max_polls=0,
        allow_simulated_model=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, app
    if closer is not None:
        await closer()


def headers():
    return {"X-API-Key": KEY}


async def test_api_hides_credentials_and_keeps_request_identity(client, tmp_path):
    http, _app = client
    work = tmp_path / "work"
    work.mkdir()
    denied = await http.post("/v1/conversations", headers=headers(), json={
        "model_ref": "deepseek-flash",
        "protocol": "openai",
        "cwd": str(work),
        "api_key": "sk-test-secret",
    })
    assert denied.status_code == 422
    assert "sk-test-secret" not in denied.text
    missing = await http.get("/v1/conversations/missing", headers={"X-API-Key": "wrong-key"})
    assert missing.status_code == 401
    assert "wrong-key" not in missing.text
    created = await http.post("/v1/conversations", headers=headers(), json={
        "model_ref": "deepseek-flash",
        "protocol": "openai",
        "cwd": str(work),
    })
    assert created.status_code == 201
    conversation_id = created.json()["id"]
    assert "api_key" not in created.json()
    first = await http.post(f"/v1/conversations/{conversation_id}/messages", headers=headers(), json={
        "body": "hello",
        "client_request_id": "same",
        "budget": {"max_calls": 1, "api_key": "sk-budget-secret"},
    })
    again = await http.post(f"/v1/conversations/{conversation_id}/messages", headers=headers(), json={
        "body": "hello",
        "client_request_id": "same",
    })
    conflict = await http.post(f"/v1/conversations/{conversation_id}/messages", headers=headers(), json={
        "body": "different",
        "client_request_id": "same",
    })
    busy = await http.post(f"/v1/conversations/{conversation_id}/messages", headers=headers(), json={
        "body": "another",
        "client_request_id": "other",
    })
    assert first.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert busy.status_code == 409
    assert "sk-budget-secret" not in json.dumps(first.json())
    fetched = await http.get(f"/v1/runs/{first.json()['id']}", headers=headers())
    assert fetched.json()["status"] == "running"
    assert "sk-budget-secret" not in fetched.text


async def test_events_sse_and_controls(client, tmp_path):
    http, app = client
    work = tmp_path / "ctrl"
    work.mkdir()
    created = await http.post("/v1/conversations", headers=headers(), json={
        "model_ref": "deepseek-flash",
        "protocol": "openai",
        "cwd": str(work),
        "confirm_strategy": "always_confirm",
    })
    conversation_id = created.json()["id"]
    message = await http.post(f"/v1/conversations/{conversation_id}/messages", headers=headers(), json={
        "body": "写入",
        "client_request_id": "write-1",
    })
    run_id = message.json()["id"]
    app.state.coordinator.configure(conversation_id, PolicyConfig())
    simulated = await http.post(
        f"/v1/conversations/{conversation_id}/runs/{run_id}/simulated-response",
        headers=headers(),
        json={"response": {
            "id": "resp-write",
            "choices": [{"message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({"file_path": "note.txt", "content": "x"})},
                }],
            }}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }},
    )
    assert simulated.status_code == 200
    approval = simulated.json()["pending_approvals"][0]
    paused = await http.post(f"/v1/runs/{run_id}/pause", headers=headers())
    assert paused.status_code == 200
    events = await http.get(
        f"/v1/conversations/{conversation_id}/events",
        headers=headers(),
        params={"after_seq": 1, "limit": 2},
    )
    assert events.status_code == 200
    assert events.json()["events"]
    assert all(item["seq"] > 1 for item in events.json()["events"])
    complete = await http.get(
        f"/v1/conversations/{conversation_id}/events",
        headers=headers(),
        params={"after_seq": 0, "limit": 500},
    )
    last_seq = complete.json()["next_seq"]
    async with http.stream(
        "GET",
        f"/v1/conversations/{conversation_id}/events/stream",
        headers={**headers(), "Last-Event-ID": "0"},
        params={"after_seq": last_seq},
    ) as stream:
        assert await stream.aread() == b""
    async with http.stream(
        "GET",
        f"/v1/conversations/{conversation_id}/events/stream",
        headers={**headers(), "Last-Event-ID": "0"},
        params={"after_seq": 1},
    ) as stream:
        replay = (await stream.aread()).decode()
    assert "id: 1\n" not in replay
    assert f"id: {last_seq}\n" in replay
    decided = await http.post(
        f"/v1/runs/{run_id}/approvals/{approval['id']}",
        headers=headers(),
        json={"decision": "reject", "arguments_hash": approval["arguments_hash"]},
    )
    assert decided.status_code == 200
    assert decided.json()["decision"] == "reject"
    assert not (work / "note.txt").exists()
    terminal = await http.post(f"/v1/runs/{run_id}/resume", headers=headers())
    assert terminal.status_code in {200, 409}


async def test_skill_memory_eval_and_attachment(client, tmp_path):
    http, app = client
    work = tmp_path / "skill"
    work.mkdir()
    created = await http.post("/v1/conversations", headers=headers(), json={
        "model_ref": "deepseek-flash",
        "protocol": "openai",
        "cwd": str(work),
    })
    conversation_id = created.json()["id"]
    mode = await http.post(f"/v1/conversations/{conversation_id}/mode", headers=headers(), json={"mode": "plan"})
    assert mode.json()["mode"] == "plan"
    blocked = await http.post(f"/v1/conversations/{conversation_id}/mode", headers=headers(), json={"mode": "default"})
    assert blocked.status_code == 409
    extracted = await http.post(
        f"/v1/conversations/{conversation_id}/skill-extractions",
        headers=headers(),
        json={
            "name": "read-before-edit",
            "content": "先读取文件",
            "source": "skill_create",
            "evidence": [
                {"kind": "user_feedback", "event_id": "u1"},
                {"kind": "user_feedback", "event_id": "u2"},
            ],
        },
    )
    version_id = extracted.json()["id"]
    listed = await http.get("/v1/skills", headers=headers(), params={"status": "proposed"})
    assert listed.json()["skills"][0]["versions"][0]["id"] == version_id
    approved = await http.post(f"/v1/skills/{listed.json()['skills'][0]['id']}/decision", headers=headers(), json={
        "version_id": version_id,
        "content_hash": extracted.json()["content_hash"],
        "decision": "approve",
        "suite_ref": "evals/suite.json",
    })
    assert approved.json()["status"] == "approved"
    detail = await http.get(
        f"/v1/skills/{listed.json()['skills'][0]['id']}/versions/{version_id}",
        headers=headers(),
    )
    assert detail.json()["content"] == "先读取文件"
    memories = await http.get("/v1/memories", headers=headers(), params={"conversation_id": conversation_id})
    assert memories.json()["memories"] == {}
    started = await http.post("/v1/evals", headers=headers(), json={
        "version_id": version_id,
        "suite_ref": "evals/suite.json",
        "model_ref": "deepseek-flash",
        "parameters": {"thinking": "disabled", "api_key": "sk-eval-secret"},
    })
    assert started.status_code == 201
    assert started.json()["status"] == "running"
    assert "sk-eval-secret" not in started.text
    latest = await http.get("/v1/evals/latest", headers=headers())
    assert latest.json()["id"] == started.json()["id"]
    app.state.coordinator.attachments["att-1"] = {"id": "att-1", "bytes": b"hello", "size": 5, "hash": "abc"}
    attachment = await http.get("/v1/attachments/att-1", headers=headers())
    assert attachment.status_code == 200
    assert attachment.content == b"hello"
    absent = await http.get("/v1/attachments/missing", headers=headers())
    assert absent.status_code == 404


def test_console_and_cli_do_not_expose_a_key():
    page = (ROOT / "forge" / "static" / "console.html").read_text(encoding="utf-8")
    assert "innerHTML" not in page
    assert "localStorage" not in page
    assert main(["serve", "--host", "0.0.0.0", "--db", "x.sqlite", "--scope", "/tmp"]) == 2
    assert main(["--api-key", "memory-only", "resume", "conversation-123"]) == 0


async def test_phase6_demos(tmp_path):
    result = await run_demos(tmp_path)
    assert result["file_exists"] is False
    assert result["rejected_status"] == "succeeded"
    assert result["folded"] is True
    assert result["expired_approval"] == 410
