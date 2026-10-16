from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.c_suite import materialize, run_comparison, run_c_suite, score_task, scores_from_report
from forge.coordinator import Coordinator
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from forge.skills import SkillService
from scripts.run_live_eval import RUN_CAP_USD, _cost_cap, _optional_text, main


def _reply(text: str, prompt_tokens: int = 10, completion_tokens: int = 5, tool_calls=None):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "call-1",
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


async def _run(tmp_path, tasks, poster, **kwargs):
    kernel = Kernel(MemoryEventStore())
    return await run_c_suite(
        kernel,
        Coordinator(kernel),
        tasks,
        poster,
        work_root=tmp_path / "work",
        usd_per_million=1,
        **kwargs,
    )


async def test_readonly_task_passes_without_a_network_call(tmp_path):
    seen = []

    async def poster(request):
        seen.append(request)
        return _reply("常量在 limits.py")

    report = await _run(tmp_path, [{
        "id": "D3",
        "prompt": "回答 MAX_RETRIES 定义在哪个文件。不要修改文件。",
        "fixture": "三个模块中，常量 MAX_RETRIES = 4 定义在 limits.py。",
        "assertions": ["最终答复包含 limits.py", "工作区文件哈希全部不变"],
    }], poster)
    assert report["complete"] is True
    assert report["promoted"] is False
    assert report["tasks"][0]["passed"] is True
    assert seen[0]["thinking"] == {"type": "disabled"}
    assert "api_key" not in json.dumps(seen[0])
    assert (tmp_path / "work" / "D3" / "none" / "limits.py").read_text(encoding="utf-8") == "MAX_RETRIES = 4\n"


async def test_cost_cap_stops_before_the_next_task(tmp_path):
    calls = []

    async def poster(request):
        calls.append(request["messages"][-1]["content"])
        return _reply("ok", prompt_tokens=20_000_000, completion_tokens=0)

    report = await _run(tmp_path, [
        {"id": "T1", "prompt": "第一题", "assertions": ["最终答复包含 ok"]},
        {"id": "T2", "prompt": "第二题", "assertions": ["最终答复包含 ok"]},
    ], poster, cost_cap_usd=15)
    assert calls == ["第一题"]
    assert report["complete"] is False
    assert report["reason"] == "over_cap"
    assert report["promoted"] is False
    assert "api_key" not in json.dumps(report)


async def test_unrecognized_assertion_does_not_promote(tmp_path):
    async def poster(request):
        return _reply("done")

    report = await _run(tmp_path, [{
        "id": "HX",
        "prompt": "做一件事",
        "assertions": ["某个未登记的检查"],
    }], poster)
    assert report["complete"] is False
    assert report["reason"] == "unscored"
    assert report["promoted"] is False


async def test_comparison_uses_separate_dirs_and_promotes_only_with_a_runtime_pass(tmp_path):
    calls = []

    async def poster(request):
        system = request["messages"][0]["content"]
        calls.append(system)
        last = request["messages"][-1]["content"]
        if "limits.py" in last:
            return _reply("limits.py")
        return _reply("beta" if "SKILL-MARKER" in system else "plain")

    kernel = Kernel(MemoryEventStore())
    report = await run_comparison(
        kernel,
        Coordinator(kernel),
        poster,
        dev_tasks=[{
            "id": "D3",
            "prompt": "回答 MAX_RETRIES 定义在哪个文件。",
            "fixture": "常量在 limits.py。",
            "assertions": ["最终答复包含 limits.py", "工作区文件哈希全部不变"],
        }],
        holdout_tasks=[{
            "id": "H-local",
            "prompt": "说出暗号",
            "assertions": ["最终答复包含 beta"],
        }],
        work_root=tmp_path / "work",
        usd_per_million=1,
        candidate="SKILL-MARKER",
    )
    assert len(calls) == 4
    assert any("SKILL-MARKER" in call for call in calls)
    assert any("SKILL-MARKER" not in call for call in calls)
    none_dir = tmp_path / "work" / "holdout" / "H-local" / "none"
    candidate_dir = tmp_path / "work" / "holdout" / "H-local" / "candidate"
    assert none_dir.is_dir() and candidate_dir.is_dir() and none_dir != candidate_dir
    assert not (tmp_path / "work" / "holdout" / "H-local" / "active").exists()
    assert "说出暗号" not in json.dumps(report)
    scores = scores_from_report(report, a_passed=1, a_total=1)
    assert scores["holdout_candidate"] == 1
    assert scores["holdout_none"] == 0
    assert "holdout_active" not in scores
    promoted = await _finish(tmp_path / "promote", scores)
    assert promoted.promoted is True
    blocked = await _finish(tmp_path / "blocked", scores_from_report(report, a_passed=0, a_total=0))
    assert blocked.promoted is False
    assert blocked.reason == "runtime_failed"


async def test_equal_holdout_is_a_tie(tmp_path):
    async def poster(request):
        return _reply("beta")

    kernel = Kernel(MemoryEventStore())
    report = await run_comparison(
        kernel,
        Coordinator(kernel),
        poster,
        dev_tasks=[],
        holdout_tasks=[{"id": "H-local", "prompt": "说出暗号", "assertions": ["最终答复包含 beta"]}],
        work_root=tmp_path / "work",
        usd_per_million=1,
        candidate="SKILL-MARKER",
    )
    finished = await _finish(tmp_path / "tie", scores_from_report(report, a_passed=1, a_total=1))
    assert finished.promoted is False
    assert finished.reason == "tie"


def test_holdout_file_is_rejected_as_a_skill(tmp_path):
    with pytest.raises(SystemExit):
        _optional_text(["--candidate-file", str(tmp_path / "holdout.json")], "--candidate-file")


async def _finish(scope, scores):
    skills = SkillService(Kernel(MemoryEventStore()), scope=str(scope))
    version = await skills.propose("read-first", "SKILL-MARKER", [
        {"kind": "user_feedback", "event_id": "u1", "text": "先读"},
        {"kind": "user_feedback", "event_id": "u2", "text": "再改"},
    ], source="add")
    version = await skills.approve(version.id, content_hash=version.content_hash, suite_ref="evals/suite.json")
    started = await skills.start_eval(version.id, model_ref="deepseek-flash", parameters={}, config_version="1")
    return await skills.complete_eval(started.id, scores, pricing_usd_per_million=1, cost_cap_usd=15)


def _workspace_bytes(root):
    found = {}
    for path in root.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts:
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _correct(task_id: str, root):
    if task_id == "H1":
        (root / "csvmini.py").write_text("def parse_row(line):\n    return line.split(\",\")\n", encoding="utf-8")
    elif task_id == "H2":
        text = (root / "tool.py").read_text(encoding="utf-8")
        needle = 'parser.add_argument("path")\n'
        (root / "tool.py").write_text(
            text.replace(needle, needle + '    parser.add_argument("--dry-run", action="store_true")\n'),
            encoding="utf-8",
        )
    elif task_id == "H4":
        (root / "table.py").write_text(
            "def render_table(rows):\n    return \"\\n\".join(\",\".join(row) for row in rows)\n",
            encoding="utf-8",
        )
        (root / "report.py").write_text(
            "from table import render_table\n\ndef load_rows():\n    return [[\"a\", \"b\"]]\n",
            encoding="utf-8",
        )
    elif task_id == "H6":
        (root / "retry.py").write_text(
            "def call_until(fn, attempts):\n"
            "    if attempts < 1:\n"
            "        raise ValueError(\"attempts\")\n"
            "    error = None\n"
            "    for _ in range(attempts):\n"
            "        try:\n"
            "            return fn()\n"
            "        except Exception as exc:\n"
            "            error = exc\n"
            "    raise error\n",
            encoding="utf-8",
        )
        (root / "test_retry.py").write_text(
            "import pytest\nfrom retry import call_until\n\n"
            "def test_success():\n    assert call_until(lambda: 3, 2) == 3\n\n"
            "def test_attempts():\n"
            "    with pytest.raises(ValueError):\n"
            "        call_until(lambda: 1, 0)\n",
            encoding="utf-8",
        )
    elif task_id == "H7":
        text = (root / "job.toml").read_text(encoding="utf-8").replace("workers = 2", "workers = 5")
        (root / "job.toml").write_text(text, encoding="utf-8")
    elif task_id == "H8":
        (root / "client.py").write_text(
            "def load(item_id):\n    if not item_id:\n        raise ValueError(\"missing id\")\n",
            encoding="utf-8",
        )


def test_holdout_assertions_are_scored(tmp_path):
    suite = json.loads((Path(__file__).resolve().parents[1] / "evals" / "holdout.json").read_text(encoding="utf-8"))
    replies = {
        "H3": "调用方是 mailer.py 和 webhook.py。draft.py 不是调用方。",
        "H5": "没有执行 git push。",
    }
    for task in suite["tasks"]:
        root = tmp_path / task["id"]
        materialize(task["id"], root)
        before = _workspace_bytes(root)
        _correct(task["id"], root)
        scored = score_task(task, root, before, replies.get(task["id"], ""), [])
        assert [item["ok"] for item in scored] == [True] * len(scored), task["id"]


def test_confirmed_run_defaults_to_a_five_dollar_stop():
    assert RUN_CAP_USD == 5
    assert _cost_cap(["prog"], RUN_CAP_USD) == 5
    assert _cost_cap(["prog", "--cost-cap", "5"], 15) == 5
    assert _cost_cap(["prog", "--cost-cap", "0"], 5) is None


def test_manual_entry_still_refuses_to_connect(tmp_path):
    assert main(["prog"], {}) == 2
    assert main(["prog", "--confirm-live"], {"FORGE_USD_PER_MILLION": "1.20", "APIKEY": "not-sent"}) == 0


def test_dev_fixture_scoring_covers_an_edited_return(tmp_path):
    root = tmp_path / "D1"
    materialize("D1", root)
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    source = (root / "calc.py").read_text(encoding="utf-8").replace("a - b", "a + b")
    (root / "calc.py").write_text(source, encoding="utf-8")
    scored = score_task(
        {"assertions": ["测试通过", "calc.py 的 diff 只触及返回表达式", "测试文件不变"]},
        root,
        before,
        "",
        [],
    )
    assert [item["ok"] for item in scored] == [True, True, True]
