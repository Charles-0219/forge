from __future__ import annotations

import json

from forge.c_suite import materialize, run_c_suite, score_task
from forge.coordinator import Coordinator
from forge.kernel import Kernel
from forge.memory_store import MemoryEventStore
from scripts.run_live_eval import main


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
