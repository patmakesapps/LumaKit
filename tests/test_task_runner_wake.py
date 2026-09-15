"""The runner wakes immediately on task creation and on approval instead of
sleeping out the rest of its tick interval."""

import time

import pytest

import core.task_store as ts
from core import task_approvals
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
        return {"message": step}


def _tool_call(name, arguments):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
    }


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "DB_PATH", tmp_path / "tasks.db")
    return ts


def _make_runner(monkeypatch, llm, tmp_path):
    runner = TaskRunner(interval=3600, notify=lambda msg, cid=None, meta=None: None)
    monkeypatch.setattr(runner, "_get_ollama", lambda: llm)
    monkeypatch.setattr(runner, "_get_registry", lambda: None)
    monkeypatch.setattr(runner, "_build_tool_list", lambda registry: [])
    monkeypatch.setattr(
        runner, "_model_config",
        lambda owner_chat_id=None: {"primary_model": "scripted", "fallback_model": None},
    )
    monkeypatch.setattr(runner, "_resolve_task_workspace_or_fallback", lambda task: tmp_path)
    monkeypatch.setattr(
        runner, "_run_tool",
        lambda task_id, name, inputs: {"success": True, "data": {"ran": name}},
    )
    monkeypatch.setattr(runner, "_record_changed_files", lambda *a, **k: None)
    return runner


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_runner_wakes_on_create_and_on_approval(store, tmp_path, monkeypatch):
    llm = ScriptedLLM([
        _tool_call("git_commit", {"message": "needs approval"}),
        _tool_call("finish_task", {"outcome": "done", "report": "committed"}),
    ])
    runner = _make_runner(monkeypatch, llm, tmp_path)
    runner.start()  # interval is an hour — only wake-ups can move things
    try:
        time.sleep(0.2)  # let the first (empty) tick pass and the loop go to sleep
        task_id = store.create_task(
            title="wake me", goal="commit", owner_chat_id="owner", workspace_path=str(tmp_path),
        )
        assert _wait_for(lambda: store.get_task(task_id)["status"] == "blocked"), \
            "task was not picked up promptly after creation"

        ok, _ = task_approvals.approve(task_id)
        assert ok
        assert _wait_for(lambda: store.get_task(task_id)["status"] == "done"), \
            "task did not resume promptly after approval"
    finally:
        started = time.monotonic()
        runner.stop()
        # stop() must not wait out the sleep interval either.
        assert time.monotonic() - started < 5
