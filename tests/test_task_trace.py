"""Task traces: one readable JSONL record per autonomous task, written by
the runner as it works, with sizes/previews instead of full payloads."""

import json
import os
import time

import pytest

import core.task_store as ts
from core import task_trace
from core.task_runner import TaskRunner


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.fallback_model = None

    def tags(self, request_timeout=None):
        return {}

    def chat(self, model, messages, **kwargs):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return {"message": step, "prompt_eval_count": 1200, "eval_count": 80}


def _tool_call(name, arguments):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
    }


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # traces_dir is already redirected to a temp folder by the autouse
    # fixture in conftest.py.
    monkeypatch.setattr(ts, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.delenv("LUMAKIT_TASK_TRACES", raising=False)
    return ts


def _make_runner(monkeypatch, llm, tmp_path, tool_result):
    runner = TaskRunner(notify=lambda msg, cid: None)
    monkeypatch.setattr(runner, "_get_ollama", lambda: llm)
    monkeypatch.setattr(runner, "_get_registry", lambda: None)
    monkeypatch.setattr(runner, "_build_tool_list", lambda registry: [])
    monkeypatch.setattr(
        runner, "_model_config",
        lambda owner_chat_id=None: {"primary_model": "scripted", "fallback_model": None},
    )
    monkeypatch.setattr(runner, "_resolve_task_workspace_or_fallback", lambda task: tmp_path)
    monkeypatch.setattr(runner, "_run_tool", lambda task_id, name, inputs: tool_result)
    return runner


def _pytest_output():
    body = "\n".join(f"tests/test_x.py:{i}: assert 1 == 2" for i in range(800))
    return body + "\nFAILED tests/test_x.py::test_total - AssertionError\n1 failed, 4 passed in 0.2s\n"


def test_trace_records_the_whole_run(store, tmp_path, monkeypatch):
    task_id = store.create_task(
        title="fix the failing test", goal="run pytest, fix the bug, verify",
        owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    big_result = {
        "success": False,
        "data": {"command": "python -m pytest -q", "returncode": 1,
                 "stdout": _pytest_output(), "stderr": ""},
    }
    llm = ScriptedLLM([
        _tool_call("update_todos", {"todos": [{"description": "run tests", "status": "in_progress"}]}),
        _tool_call("run_command", {"command": "python -m pytest -q"}),
        _tool_call("finish_task", {"outcome": "done", "report": "Fixed the off-by-one and tests pass."}),
    ])
    runner = _make_runner(monkeypatch, llm, tmp_path, big_result)
    runner._tick()

    assert store.get_task(task_id)["status"] == "done"
    path = task_trace.trace_path(task_id)
    assert path.exists()
    events = task_trace.read(task_id)
    kinds = [e["event"] for e in events]

    assert kinds[0] == "task_started"
    assert events[0]["title"] == "fix the failing test"
    assert "drive_started" in kinds
    assert kinds.count("round") == 3
    assert kinds[-1] == "finished"
    assert events[-1]["outcome"] == "done"
    assert "off-by-one" in events[-1]["report"]

    rounds = [e for e in events if e["event"] == "round"]
    assert all(r["prompt_tokens"] == 1200 and r["completion_tokens"] == 80 for r in rounds)
    assert all(isinstance(r["latency_ms"], int) for r in rounds)

    tools = [e for e in events if e["event"] == "tool"]
    assert [t["name"] for t in tools] == ["update_todos", "run_command"]
    cmd = tools[1]
    assert cmd["success"] is False
    assert "pytest" in cmd["args"]
    assert cmd["trimmed"] is True
    assert cmd["stored_chars"] < cmd["result_chars"]
    # Previews only: the trace must stay small even when the tool output is huge.
    assert path.stat().st_size < 8_000
    assert len(cmd["result"]) <= task_trace.RESULT_PREVIEW_CHARS + 40

    # Every line is standalone JSON.
    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_render_and_listing(store, tmp_path, monkeypatch):
    task_id = store.create_task(
        title="one shot", goal="do it", owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    llm = ScriptedLLM([_tool_call("finish_task", {"outcome": "done", "report": "done in one"})])
    _make_runner(monkeypatch, llm, tmp_path, {"success": True, "data": {}})._tick()

    text = task_trace.render(task_id)
    assert "Task %d: one shot" % task_id in text
    assert "FINISHED: DONE" in text
    assert "rounds:        1" in text

    rows = task_trace.list_traces()
    assert rows and rows[0]["task_id"] == task_id
    assert rows[0]["outcome"] == "done"
    assert rows[0]["title"] == "one shot"

    assert "No trace for task 999" in task_trace.render(999)


def test_tracing_can_be_disabled(store, tmp_path, monkeypatch):
    monkeypatch.setenv("LUMAKIT_TASK_TRACES", "0")
    task_id = store.create_task(
        title="quiet", goal="no trace", owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    llm = ScriptedLLM([_tool_call("finish_task", {"outcome": "done", "report": "ok"})])
    _make_runner(monkeypatch, llm, tmp_path, {"success": True, "data": {}})._tick()
    assert store.get_task(task_id)["status"] == "done"
    assert not task_trace.trace_path(task_id).exists()


def test_start_clears_a_stale_trace_with_the_same_id(store, tmp_path, monkeypatch):
    task_id = store.create_task(
        title="fresh", goal="new db, old id", owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    task_trace.trace_path(task_id).write_text('{"event":"finished","outcome":"done"}\n', encoding="utf-8")
    llm = ScriptedLLM([_tool_call("finish_task", {"outcome": "done", "report": "ok"})])
    _make_runner(monkeypatch, llm, tmp_path, {"success": True, "data": {}})._tick()
    events = task_trace.read(task_id)
    assert events[0]["event"] == "task_started"
    assert [e["event"] for e in events].count("finished") == 1


def test_prune_removes_old_traces_only(store, tmp_path):
    old = task_trace.trace_path(1)
    new = task_trace.trace_path(2)
    old.write_text('{"event":"x"}\n', encoding="utf-8")
    new.write_text('{"event":"x"}\n', encoding="utf-8")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))

    assert task_trace.prune(days=30) == 1
    assert not old.exists()
    assert new.exists()
    assert task_trace.prune(days=0) == 0  # retention off → nothing removed
