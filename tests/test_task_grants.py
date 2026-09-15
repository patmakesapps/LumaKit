"""Scoped standing grants: an owner can allow a category of protected action
for one whole task, up front or when the first approval request arrives.
Everything not granted still pauses the task for a one-shot approval."""

import pytest

import core.task_store as ts
from core import task_approvals
from core.approval_policy import normalize_task_actions, task_action_catalog, task_action_key
from core.task_runner import TaskRunner


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.fallback_model = None

    def tags(self, request_timeout=None):
        return {}

    def chat(self, model, messages, **kwargs):
        self.last_thread = [dict(m) for m in messages]
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


def _make_runner(monkeypatch, llm, tmp_path, events):
    def notify(msg, chat_id=None, meta=None):
        events.append(meta or {"event": "text", "text": msg})

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
        lambda task_id, name, inputs: {"success": True, "data": {"ran": name}},
    )
    monkeypatch.setattr(runner, "_record_changed_files", lambda *a, **k: None)
    return runner


def test_action_keys_cover_tools_and_shell_commands():
    assert task_action_key("git_commit", {}) == "git_commit"
    assert task_action_key("git_add", {}) == "git_commit"
    assert task_action_key("git_push", {}) == "git_push"
    assert task_action_key("delete_file", {"path": "x"}) == "delete_files"
    assert task_action_key("run_command", {"command": "git commit -m fix"}) == "git_commit"
    assert task_action_key("run_command", {"command": "git push origin main"}) == "git_push"
    assert task_action_key("run_command", {"command": "rm -rf build"}) == "delete_files"
    assert task_action_key("run_command", {"command": "pytest -q"}) is None
    assert task_action_key("read_file", {"path": "x"}) is None
    assert normalize_task_actions(["git_commit", "bogus", "git_commit", "git_push"]) == ["git_commit", "git_push"]
    assert {a["key"] for a in task_action_catalog()} >= {"git_commit", "git_push", "delete_files"}


def test_pre_approved_action_runs_without_pausing(store, tmp_path, monkeypatch):
    events = []
    task_id = store.create_task(
        title="commit freely", goal="fix and commit",
        constraints={"_allowed_actions": ["git_commit"]},
        owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    llm = ScriptedLLM([
        _tool_call("git_commit", {"message": "first"}),
        _tool_call("run_command", {"command": "git commit -m second"}),
        _tool_call("git_push", {"remote": "origin"}),  # NOT granted → pauses
    ])
    runner = _make_runner(monkeypatch, llm, tmp_path, events)
    runner._tick()

    task = store.get_task(task_id)
    assert task["status"] == "blocked"
    pending = task_approvals.pending_approval(task)
    assert pending["tool"] == "git_push"
    assert pending["action_key"] == "git_push"
    used = [h for h in task["history"] if h.get("type") == "standing_grant_used"]
    assert [h["tool"] for h in used] == ["git_commit", "run_command"]
    # Only one approval ping, for the push — never for the commits.
    approvals = [e for e in events if e.get("event") == "approval"]
    assert len(approvals) == 1 and approvals[0]["tool"] == "git_push"
    # The kickoff told the model what was pre-approved.
    kickoff = next(m for m in llm.last_thread if m.get("role") == "user")
    assert "Pre-approved for this task" in kickoff["content"]
    assert "Git add & commit" in kickoff["content"]


def test_approve_for_task_stops_future_pauses(store, tmp_path, monkeypatch):
    events = []
    task_id = store.create_task(
        title="two commits", goal="commit twice", owner_chat_id="owner",
        workspace_path=str(tmp_path),
    )
    llm = ScriptedLLM([
        _tool_call("git_commit", {"message": "one"}),
        _tool_call("git_commit", {"message": "two"}),
        _tool_call("finish_task", {"outcome": "done", "report": "both committed"}),
    ])
    runner = _make_runner(monkeypatch, llm, tmp_path, events)
    runner._tick()
    assert store.get_task(task_id)["status"] == "blocked"

    ok, message = task_approvals.approve(task_id, scope="task")
    assert ok and "rest of this task" in message
    assert task_approvals.allowed_actions(store.get_task(task_id)) == ["git_commit"]

    runner._tick()
    task = store.get_task(task_id)
    assert task["status"] == "done"
    # The second commit went through on the standing grant, no second pause.
    assert sum(1 for e in events if e.get("event") == "approval") == 1
    assert any(h.get("type") == "standing_grant_used" for h in task["history"])


def test_set_allowed_actions_validates_and_logs(store, tmp_path):
    task_id = store.create_task(
        title="perms", goal="g", owner_chat_id="owner", workspace_path=str(tmp_path),
    )
    assert task_approvals.set_allowed_actions(task_id, ["git_push", "nope"]) == ["git_push"]
    assert task_approvals.allowed_actions(store.get_task(task_id)) == ["git_push"]
    assert task_approvals.set_allowed_actions(task_id, []) == []
    assert task_approvals.allowed_actions(store.get_task(task_id)) == []
    changes = [h for h in store.get_task(task_id)["history"] if h.get("type") == "permissions_changed"]
    assert len(changes) == 2
    assert "allowed Git push" in changes[0]["detail"]
    assert "revoked Git push" in changes[1]["detail"]
