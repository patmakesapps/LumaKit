"""Task lifecycle events carry structured metadata so surfaces can render
cards (web) and log them, while plain (msg, chat_id) callbacks keep working."""

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


def _make_runner(monkeypatch, llm, tmp_path, notify):
    runner = TaskRunner(notify=notify)
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
        lambda task_id, name, inputs: {"success": True, "data": {"committed": True}},
    )
    monkeypatch.setattr(runner, "_record_changed_files", lambda *a, **k: None)
    return runner


def test_events_carry_metadata_through_approval_and_completion(store, tmp_path, monkeypatch):
    events = []

    def notify(msg, chat_id=None, meta=None):
        events.append((msg, chat_id, meta))

    task_id = store.create_task(
        title="fix the tests", goal="fix and commit", owner_chat_id="owner",
        workspace_path=str(tmp_path),
    )
    # git_commit is a protected action inside tasks → pauses for approval.
    llm = ScriptedLLM([
        _tool_call("git_commit", {"message": "Fix the off-by-one"}),
        _tool_call("finish_task", {"outcome": "done", "report": "Committed the fix."}),
    ])
    runner = _make_runner(monkeypatch, llm, tmp_path, notify)
    runner._tick()

    kinds = [m["event"] for _, _, m in events if m]
    assert kinds == ["started", "approval"]
    started = events[0][2]
    assert started["kind"] == "task" and started["task_id"] == task_id
    assert started["title"] == "fix the tests"
    assert "fix and commit" in started["goal"]
    approval = events[1][2]
    assert approval["tool"] == "git_commit"
    assert "Fix the off-by-one" in approval["summary"]
    assert events[1][1] == "owner"
    assert store.get_task(task_id)["status"] == "blocked"

    ok, _ = task_approvals.approve(task_id)
    assert ok
    runner._tick()

    assert store.get_task(task_id)["status"] == "done"
    done = events[-1][2]
    assert done["event"] == "done"
    assert done["report"] == "Committed the fix."
    assert isinstance(done["files_changed"], list)
    assert "Task complete" in events[-1][0]


def test_two_argument_notify_callbacks_still_work(store, tmp_path, monkeypatch):
    messages = []
    task_id = store.create_task(
        title="one shot", goal="do it", owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    llm = ScriptedLLM([_tool_call("finish_task", {"outcome": "done", "report": "ok"})])
    runner = _make_runner(monkeypatch, llm, tmp_path, lambda msg, cid: messages.append(msg))
    runner._tick()
    assert store.get_task(task_id)["status"] == "done"
    assert any(m.startswith("Starting task") for m in messages)
    assert any(m.startswith("Task complete") for m in messages)


def test_chat_reply_resolves_pending_approval(store, tmp_path, monkeypatch):
    """The web chat's fast path: '1' / 'yes' / '/approve N' answer the pending
    approval without a model call."""
    from surfaces import web

    task_id = store.create_task(
        title="needs a commit", goal="commit", owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    assert web._task_approval_reply("1") is None  # nothing pending yet

    task_approvals.request_approval(store.get_task(task_id), "git_commit", {"message": "m"})
    assert web._task_approval_reply("1") == ("approve", task_id)
    assert web._task_approval_reply("  YES ") == ("approve", task_id)
    assert web._task_approval_reply("no") == ("deny", task_id)
    assert web._task_approval_reply(f"/deny {task_id}") == ("deny", task_id)
    assert web._task_approval_reply("/approve 42") == ("approve", 42)
    assert web._task_approval_reply("what is the status?") is None


def test_task_event_persists_into_origin_chat(store, tmp_path, monkeypatch):
    from core import chat_store
    from surfaces import web

    monkeypatch.setattr(chat_store, "DB_PATH", tmp_path / "memory.db")

    chat_id = chat_store.new_chat_id()
    chat_store.save_chat(
        chat_id, "the origin chat",
        [{"role": "user", "content": "make a task"}],
        owner_id=web.WEB_USER_ID,
        display_messages=[{"role": "user", "content": "make a task"}],
    )
    task_id = store.create_task(
        title="t", goal="g", constraints={"_origin_chat": chat_id},
        owner_chat_id=web.WEB_USER_ID, workspace_path=str(tmp_path),
    )
    meta = {"kind": "task", "task_id": task_id, "title": "t", "event": "approval",
            "tool": "git_commit", "summary": "git commit -m x"}
    assert web._persist_task_event(meta, "needs approval") == chat_id

    saved = chat_store.load_chat(chat_id, owner_id=web.WEB_USER_ID)
    display = saved["display_messages"]
    assert display[-1]["task"]["event"] == "approval"
    assert display[-1]["content"] == "needs approval"
    assert display[-1]["task"].get("resolution") is None

    web._resolve_task_cards(task_id, "approved")
    saved = chat_store.load_chat(chat_id, owner_id=web.WEB_USER_ID)
    assert saved["display_messages"][-1]["task"]["resolution"] == "approved"
