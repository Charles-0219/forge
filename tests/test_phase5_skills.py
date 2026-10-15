from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.errors import Conflict
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from forge.skills import (
    SkillService,
    build_live_request,
    compare_tasks,
    development_inputs,
    live_guard,
    retrieval_report,
    validate_evidence,
)
from forge.sqlite_store import SqliteEventStore
from scripts.run_live_eval import main
from tests.test_phase3_execution import open_run


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["memory", "sqlite"])
async def service(request, tmp_path):
    if request.param == "memory":
        kernel = Kernel(MemoryEventStore())
    else:
        store = SqliteEventStore(tmp_path / "phase5.sqlite")
        await store.open()
        kernel = Kernel(store)
    try:
        yield kernel, SkillService(kernel, scope=str(tmp_path / "project"))
    finally:
        if request.param == "sqlite":
            await kernel.store.close()


def _feedback():
    return [
        {"kind": "user_feedback", "event_id": "u1", "text": "先读再改"},
        {"kind": "user_feedback", "event_id": "u2", "text": "不要直接写"},
    ]


def _scores(**overrides):
    payload = {
        "a": {"passed": 4, "total": 4},
        "hard_passed": 3,
        "hard_total": 3,
        "holdout_candidate": 2,
        "holdout_none": 1,
        "holdout_active": 1,
        "complete": True,
        "cancelled": False,
        "call_failures": 0,
        "new_permission_violations": 0,
        "new_secret_failures": 0,
        "tokens": 100,
    }
    payload.update(overrides)
    return payload


def test_evidence_and_holdout_are_rejected_before_use(tmp_path):
    validate_evidence(_feedback())
    validate_evidence([{"kind": "preference", "explicit": True, "text": "以后都先读文件"}])
    with pytest.raises(Conflict):
        validate_evidence([{"kind": "user_feedback", "event_id": "u1"}])
    with pytest.raises(Conflict):
        validate_evidence([
            {"kind": "assistant_message", "event_id": "a1"},
            {"kind": "user_feedback", "event_id": "u1"},
            {"kind": "user_feedback", "event_id": "u2"},
        ])
    with pytest.raises(Conflict):
        development_inputs(ROOT / "evals" / "holdout.json")
    pack = development_inputs(ROOT / "evals" / "suite.json")
    sentinel = tmp_path / "holdout.json"
    sentinel.write_text('{"tasks":[{"prompt":"SENTINEL-HOLDOUT-TEXT"}]}', encoding="utf-8")
    with pytest.raises(Conflict):
        development_inputs(sentinel)
    assert "SENTINEL-HOLDOUT-TEXT" not in json.dumps(pack, ensure_ascii=False)
    assert [item["id"] for item in pack["dev_tasks"]] == [f"D{index}" for index in range(1, 13)]
    assert len(pack["retrieval"]) == 20


def test_retrieval_report_counts_hits_and_injections():
    skills = [
        ("S1", "read-before-edit", "读取文件之后再修改"),
        ("S2", "other", "光合作用产生氧气"),
    ]
    report = retrieval_report(
        [
            {"id": "hit", "query": "读取文件", "relevant": ["S1"]},
            {"id": "miss", "query": "外星菜单", "relevant": []},
        ],
        skills,
    )
    assert report["items"][0]["retrieved"][0] == "S1"
    assert report["recall_at_3"] == 1
    assert report["first_hit_rate"] == 1
    assert report["irrelevant_injections"] == 0
    frozen = json.loads((ROOT / "evals" / "suite.json").read_text(encoding="utf-8"))
    suite_report = retrieval_report(
        frozen["retrieval"],
        [(item["id"], item["name"], item["body"]) for item in frozen["skills"]],
    )
    assert len(suite_report["items"]) == 20
    assert suite_report["recall_at_3"] is not None
    assert isinstance(suite_report["irrelevant_injections"], int)


def test_live_request_disables_thinking_and_stays_manual():
    request = build_live_request(
        {"prompt": "回答文件名", "fixture": "常量在 limits.py"},
        "先读取文件",
        model="deepseek-flash",
    )
    assert request["model"] == "deepseek-flash"
    assert request["thinking"] == {"type": "disabled"}
    assert "api_key" not in request
    assert live_guard(["prog"], {}) == "manual"
    assert live_guard(["prog", "--confirm-live"], {}) == "pricing"
    assert live_guard(["prog", "--confirm-live"], {"FORGE_USD_PER_MILLION": "1"}) == "credential"
    assert main(["prog"], {}) == 2
    assert main(["prog", "--confirm-live"], {"FORGE_USD_PER_MILLION": "1", "APIKEY": "not-sent"}) == 0


async def test_compare_tasks_keep_conditions_apart(tmp_path):
    seen = []

    async def runner(task, body, work, context):
        seen.append(context)
        (work / "marker.txt").write_text(context["condition"], encoding="utf-8")
        return {"passed": True}

    items = await compare_tasks(
        [{"id": "D1", "prompt": "让测试通过"}],
        runner,
        candidate="candidate-skill",
        active=None,
        work_root=tmp_path,
    )
    assert {item["condition"] for item in items} == {"none", "candidate", "active"}
    assert any(item["condition"] == "active" and item["applicable"] is False for item in items)
    assert all(item["memory_writes"] is False and item["online_evolution"] is False for item in seen)
    assert len({item["workdir"] for item in items if item.get("workdir")}) == 2


async def test_unapproved_skill_is_invisible_and_baseline_survives_deletion(service):
    kernel, skills = service
    with pytest.raises(Conflict):
        await skills.propose("read-before-edit", "先读", _feedback(), source="baseline")
    proposed = await skills.propose("read-before-edit", "先读取文件", _feedback(), source="skill_create")
    assert await skills.visible() == []
    assert await skills.search("读取文件") == []
    deleted = await skills.import_candidate("read-before-edit", None, path="SKILL.md")
    assert deleted is None
    baseline = await skills.import_baseline("read-before-edit", "先读取文件再修改", authorization="owner-import")
    assert [item["version_id"] for item in await skills.visible()] == [baseline.id]
    await skills.import_candidate("read-before-edit", "外部改写", path="SKILL.md")
    assert [item["content"] for item in await skills.visible()] == ["先读取文件再修改"]
    stored = await kernel.get_conversation(skills._ledger_id)
    assert stored.model_ref == "skill-ledger"
    blob = json.dumps([event.payload for event in await kernel.list_events(skills._ledger_id)], ensure_ascii=False)
    assert "sk-" not in blob
    assert proposed.content_hash not in {item["content_hash"] for item in await skills.visible()}


async def test_approval_freezes_content_and_suite(service):
    _kernel, skills = service
    version = await skills.propose("pytest-first", "先写失败测试", _feedback(), source="add")
    with pytest.raises(Conflict):
        await skills.approve(version.id, content_hash="0" * 64, suite_ref="suite.json")
    blocked = await skills.approve(version.id, content_hash=version.content_hash, suite_ref=None)
    assert blocked.status == "approved" and blocked.block_reason == "no_applicable_suite"
    with pytest.raises(Conflict):
        await skills.start_eval(
            blocked.id,
            model_ref="deepseek-flash",
            parameters={"thinking": "disabled"},
            config_version="1",
        )
    revised = await skills.propose(
        "pytest-first",
        "先写失败测试，再改实现",
        _feedback(),
        source="skill_evolve",
        parent_version_id=version.id,
    )
    assert revised.id != version.id
    approved = await skills.approve(revised.id, content_hash=revised.content_hash, suite_ref="evals/suite.json")
    assert approved.block_reason is None
    async with skills.kernel.store.transaction() as unit:
        frozen = await unit.get_skill_version(version.id)
    assert frozen.content == "先写失败测试"


async def test_promotion_paths_and_secret_stripping(service):
    kernel, skills = service
    version = await skills.propose("narrow-diff", "只改必要的行", _feedback(), source="auto")
    approved = await skills.approve(version.id, content_hash=version.content_hash, suite_ref="evals/suite.json")
    report = await skills.start_eval(
        approved.id,
        model_ref="deepseek-flash",
        parameters={"thinking": "disabled"},
        config_version="7",
    )
    finished = await skills.complete_eval(report.id, _scores(api_key="sk-test-secret"))
    assert finished.promoted is True and finished.reason == "promoted" and finished.cost_usd is None
    assert "sk-test-secret" not in json.dumps(finished.results)
    assert [item["content"] for item in await skills.visible()] == ["只改必要的行"]
    events = json.dumps([event.payload for event in await kernel.list_events(skills._ledger_id)])
    assert "sk-test-secret" not in events

    nxt = await skills.propose("narrow-diff", "改动再小一点", _feedback(), source="merge")
    nxt = await skills.approve(nxt.id, content_hash=nxt.content_hash, suite_ref="evals/suite.json")
    tied = await skills.start_eval(nxt.id, model_ref="deepseek-flash", parameters={}, config_version="7")
    tied_report = await skills.complete_eval(tied.id, _scores(holdout_candidate=1, holdout_active=1))
    assert tied_report.promoted is False and tied_report.reason == "tie"
    assert [item["content"] for item in await skills.visible()] == ["只改必要的行"]

    weak = await skills.propose("narrow-diff", "改得太多", _feedback(), source="add")
    weak = await skills.approve(weak.id, content_hash=weak.content_hash, suite_ref="evals/suite.json")
    runtime = await skills.start_eval(weak.id, model_ref="deepseek-flash", parameters={}, config_version="7")
    failed = await skills.complete_eval(runtime.id, _scores(**{"a": {"passed": 3, "total": 4}}))
    assert failed.reason == "runtime_failed"
    async with kernel.store.transaction() as unit:
        assert (await unit.get_skill_version(weak.id)).status == "eval_failed"


async def test_changed_baseline_cap_and_export_failure(service, tmp_path):
    kernel, skills = service
    current = await skills.import_baseline("cli-argparse", "使用 argparse", authorization="owner-import")
    candidate = await skills.propose("cli-argparse", "使用 argparse 并写帮助", _feedback(), source="skill_evolve")
    candidate = await skills.approve(candidate.id, content_hash=candidate.content_hash, suite_ref="evals/suite.json")
    report = await skills.start_eval(candidate.id, model_ref="deepseek-flash", parameters={}, config_version="1")
    async with kernel.store.transaction() as unit:
        skill = await unit.get_skill(candidate.skill_id)
        skill.active_version_id = "changed-during-eval"
        await unit.update_skill(skill)
    changed = await skills.complete_eval(report.id, _scores(holdout_candidate=3, holdout_active=0))
    assert changed.promoted is False and changed.reason == "baseline_changed"
    async with kernel.store.transaction() as unit:
        assert (await unit.get_skill_version(candidate.id)).status == "approved"
        assert (await unit.get_skill(candidate.skill_id)).active_version_id == "changed-during-eval"

    async with kernel.store.transaction() as unit:
        skill = await unit.get_skill(candidate.skill_id)
        skill.active_version_id = current.id
        await unit.update_skill(skill)
    again = await skills.propose("cli-argparse", "帮助文字保持缺省值", _feedback(), source="add")
    again = await skills.approve(again.id, content_hash=again.content_hash, suite_ref="evals/suite.json")
    capped = await skills.start_eval(again.id, model_ref="deepseek-flash", parameters={}, config_version="1")
    stopped = await skills.complete_eval(
        capped.id,
        _scores(tokens=2_000_000),
        pricing_usd_per_million=10,
        cost_cap_usd=15,
    )
    assert stopped.status == "incomplete" and stopped.promoted is False
    assert stopped.cost_usd == "20.000000"

    dest = tmp_path / "export" / "SKILL.md"
    dest.parent.mkdir()
    dest.write_text("old", encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await skills.export_active("cli-argparse", dest, replace=fail_replace)
    assert dest.read_text(encoding="utf-8") == "old"
    async with kernel.store.transaction() as unit:
        skill = await unit.get_skill_by_name(skills.scope, "cli-argparse")
    assert skill.active_version_id == current.id
    assert skill.export_error == "disk full"


async def test_eval_slot_blocks_a_second_job_and_new_root_tasks(service, tmp_path):
    kernel, skills = service
    version = await skills.propose("no-destructive-shell", "拒绝删除整个目录", _feedback(), source="skill_create")
    version = await skills.approve(version.id, content_hash=version.content_hash, suite_ref="evals/suite.json")
    await skills.start_eval(version.id, model_ref="deepseek-flash", parameters={}, config_version="1")
    other = await skills.propose("behavior-preserving-doc", "只补文档", _feedback(), source="add")
    other = await skills.approve(other.id, content_hash=other.content_hash, suite_ref="evals/suite.json")
    with pytest.raises(Conflict, match="execution slot"):
        await skills.start_eval(other.id, model_ref="deepseek-flash", parameters={}, config_version="1")
    conversation = await kernel.create_conversation(
        protocol="openai",
        model_ref="deepseek-flash",
        cwd=str(tmp_path),
        mode="default",
        confirm_strategy="never_confirm",
    )
    with pytest.raises(Conflict, match="execution slot"):
        await kernel.submit_message(conversation.id, body="hello", client_request_id="slot")


async def test_running_root_task_blocks_evaluation(service, tmp_path):
    kernel, skills = service
    await open_run(kernel, tmp_path / "busy", body="busy-root")
    version = await skills.propose("no-destructive-shell", "拒绝删除整个目录", _feedback(), source="skill_create")
    version = await skills.approve(version.id, content_hash=version.content_hash, suite_ref="evals/suite.json")
    with pytest.raises(Conflict, match="root task"):
        await skills.start_eval(version.id, model_ref="deepseek-flash", parameters={}, config_version="1")
