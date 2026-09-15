"""Autonomous task runner.

A background thread that wakes every `interval` seconds and drives autonomous
tasks the way a capable engineer actually works a problem — not as a frozen,
pre-baked plan executed by a series of amnesiac sub-agents.

How a task runs
---------------
Each task is ONE continuous agent session:

1. created → status='planning' (queued, not started yet)
2. runner picks it up → seeds a single conversation thread and tells the agent
   to LOOK at the workspace first, reuse anything already there, keep a live
   TODO list, and work the goal to completion. status='active'.
3. the agent runs a continuous think → act → observe loop with the FULL tool
   set, holding all of its context the whole time (no per-step memory wipe).
   It maintains its own todo list via `update_todos`.
4. the whole thread is persisted to the task store every round, so a task that
   takes days survives restarts and resumes with full context.
5. the task only sleeps when the agent calls `wait_until` for a real external
   wait, when a transient model/runtime outage triggers backoff, or when the
   per-tick fairness budget is hit (it resumes next tick).
6. the agent calls `finish_task` when the goal is done (or genuinely can't be).
   If a due_at passes first, the runner finalizes with whatever's done.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from core import summarizer, task_store, task_trace
from core.approval_policy import autonomous_tool_refusal
from core.paths import get_data_dir, get_repo_root, workspace_context
from core.runtime_config import get_effective_config_for_user
from ollama_client import OllamaClient



# Module-level handle to the active runner so non-service code paths
# (the web API, chat tools) can read its heartbeat without plumbing the
# service object through.
_active_runner: "TaskRunner | None" = None


def get_active_runner() -> "TaskRunner | None":
    return _active_runner


RETRYABLE_STEP_RUNTIME_RE = re.compile(
    r"LLM error during round:|LLM error during step:|Cannot reach Ollama server|"
    r"Ollama stopped responding|unable to reach Ollama|model server|"
    r"connection refused|connection reset|timed out|timeout",
    re.IGNORECASE,
)


def _summarize_tool_args(name: str | None, inputs: dict) -> str:
    """One-line summary of what a tool call is about to do, for the live feed."""
    if not isinstance(inputs, dict):
        return ""
    for key in ("url", "path", "file_path", "command", "query", "pattern", "symbol", "title"):
        val = inputs.get(key)
        if val:
            text = str(val)
            return text if len(text) <= 120 else text[:117] + "..."
    if isinstance(inputs.get("args"), list) and inputs["args"]:
        joined = " ".join(str(a) for a in inputs["args"])
        return joined[:120]
    return ""


_REPORT_PROMPT = """You are writing a short final report for an autonomous task you just worked on.

Goal: {goal}
Outcome: {status}

What you did (todo list + recent activity):
{summary}

Write a concise report (3-6 sentences) in plain, direct language covering what
was accomplished, key results, and anything left unfinished and why. No headers,
no bullet points."""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class TaskRunner:
    """Background thread that drives autonomous tasks to completion as a single
    continuous agent session per task."""

    # Transient model/runtime failures to ride out before failing the task.
    MAX_RUNTIME_RETRIES = 12
    # Seconds per LLM call.
    STEP_LLM_DEADLINE = 180
    # Token budget for a work round — tight on purpose so the model acts
    # instead of writing an essay before each tool call.
    AGENTIC_NUM_PREDICT = 1536
    # Token budget for one-shot structured calls (final report, compaction).
    STRUCTURED_NUM_PREDICT = 2048
    # Most model rounds a task may run in one continuous drive before yielding
    # the runner thread (it resumes immediately next tick). Keeps tasks fair.
    MAX_ROUNDS_PER_DRIVE = 50
    # Wall-clock budget (seconds) a single task may hold the runner per tick.
    TASK_TICK_BUDGET_SECONDS = 600
    # How many times, within one drive, to re-anchor a model that stopped
    # calling tools before yielding to a fresh drive.
    MAX_NUDGES = 2
    # How many stuck cycles (model keeps going silent with work still open)
    # across drives before we stop and report the task as failed — honestly,
    # rather than falsely "done".
    MAX_STUCK_CYCLES = 4
    # Tool results bigger than this (serialized) are trimmed before going into
    # the persistent thread, so big CSV/dir/diff dumps don't bloat context and
    # make the model choke (which shows up as empty responses). Same budget as
    # the interactive agent now that trimming keeps both ends of command
    # output instead of dropping the tail.
    MAX_TOOL_RESULT_CHARS = 4000
    # How many real external waits a task may take before we treat it as stuck.
    MAX_WAITS = 60
    MIN_WAIT_MINUTES = 1
    MAX_WAIT_MINUTES = 60 * 24 * 14  # 14 days

    def __init__(
        self,
        interval: int = 60,
        notify: Callable[[str, str | None], None] | None = None,
    ):
        self._interval = interval
        self._notify = notify or (lambda msg, cid: print(f"[task] {msg}"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_locks: dict[int, threading.Lock] = {}
        self._task_locks_guard = threading.Lock()
        self._last_tick_at: datetime | None = None

        # Lazy — built on first use so we don't slow down startup
        self._ollama: OllamaClient | None = None
        self._llm_fingerprint = None
        self._registry = None
        self._code_index = None
        self._registry_root: Path | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        try:
            self._log_catch_up()
        except Exception as e:
            print(f"[task-runner] catch-up scan failed: {e}")
        removed = task_trace.prune()
        if removed:
            print(f"[task-runner] pruned {removed} old task trace(s)")
        global _active_runner
        _active_runner = self
        self._thread = threading.Thread(target=self._loop, daemon=True, name="task-runner")
        self._thread.start()

    def _log_catch_up(self) -> None:
        due = task_store.get_due_tasks()
        if due:
            print(f"[task-runner] catch-up: {len(due)} task(s) due at startup; will process on first tick")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        global _active_runner
        if _active_runner is self:
            _active_runner = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[task-runner] tick error: {e}")
            self._stop.wait(self._interval)

    def _tick(self) -> None:
        self._last_tick_at = datetime.now()
        for task in task_store.get_overdue_tasks():
            with self._lock_for(task["id"]):
                workspace = self._resolve_task_workspace_or_fallback(task)
                with workspace_context(workspace):
                    self._finalize_overdue(task)

        for task in task_store.get_due_tasks():
            if self._stop.is_set():
                break
            with self._lock_for(task["id"]):
                workspace = self._resolve_task_workspace_or_fallback(task)
                with workspace_context(workspace):
                    status = task["status"]
                    if status == "planning" or (status == "active" and not task.get("messages")):
                        self._begin_task(task)
                    elif status == "active":
                        self._drive(task)

    def _lock_for(self, task_id: int) -> threading.Lock:
        with self._task_locks_guard:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = threading.Lock()
                self._task_locks[task_id] = lock
        return lock

    def get_last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    # ------------------------------------------------------------------
    # Task start: seed the continuous session
    # ------------------------------------------------------------------

    def _begin_task(self, task: dict) -> None:
        task_id = task["id"]
        print(f"[task-runner] starting task {task_id}: {task['title']}")

        constraints = {k: v for k, v in (task.get("constraints") or {}).items()
                       if not str(k).startswith("_")}
        constraints_str = json.dumps(constraints) if constraints else "none"

        system = self._build_system_prompt()
        kickoff = (
            f"GOAL: {task['goal']}\n\n"
            f"Title: {task['title']}\n"
            f"Constraints: {constraints_str}\n"
            f"Deadline: {task.get('due_at') or 'no hard deadline'}\n"
            f"Workspace (your working directory): {get_repo_root()}\n"
            f"Current date/time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            "Work this goal to completion on your own. Start by looking at the "
            "current state of the workspace — list what's already there and read "
            "anything relevant — so you reuse existing work instead of redoing it. "
            "Then lay out a todo list with update_todos and execute it, keeping the "
            "list current as you go. Call finish_task when the whole goal is done."
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": kickoff},
        ]
        task_store.update_task(task_id, status="active")
        task_store.save_session(task_id, messages, plan=[], current_step=0)
        task_store.append_history(task_id, {"type": "task_started"})
        task_trace.reset(task_id)
        task_trace.record(
            task_id, "task_started",
            title=task["title"], goal=task_trace.preview(task["goal"], 1000),
            constraints=constraints or None, workspace=str(get_repo_root()),
        )
        self._notify(
            f"Starting task: {task['title']}\n\nI'll work it start to finish and "
            "report back when it's done.",
            task.get("owner_chat_id"),
        )

        fresh = task_store.get_task(task_id)
        if fresh and fresh["status"] == "active" and not self._stop.is_set():
            self._drive(fresh)

    # ------------------------------------------------------------------
    # Continuous drive loop
    # ------------------------------------------------------------------

    # Persist the live message thread every N rounds instead of every round —
    # rewriting the whole JSON blob per round is O(n) write amplification on
    # long tasks (R-5). Every exit path still checkpoints, so at most the last
    # few rounds of *context* (never history/results) are re-done after a hard
    # crash mid-drive.
    SESSION_CHECKPOINT_EVERY = 5

    def _drive(self, task: dict) -> None:
        task_id = task["id"]
        drive_started = time.monotonic()
        rounds = 0
        nudges = 0
        messages: list | None = None
        rounds_since_checkpoint = 0

        def checkpoint(force: bool = True) -> None:
            nonlocal rounds_since_checkpoint
            if messages is None:
                return
            if force or rounds_since_checkpoint >= self.SESSION_CHECKPOINT_EVERY:
                task_store.save_session(task_id, messages)
                rounds_since_checkpoint = 0

        ollama = self._get_ollama()
        registry = self._get_registry()
        tools = self._build_tool_list(registry)
        cfg = self._model_config(task.get("owner_chat_id"))
        model = cfg["primary_model"]
        ollama.fallback_model = cfg.get("fallback_model")
        task_trace.record(
            task_id, "drive_started",
            model=model, fallback=cfg.get("fallback_model"), tools=len(tools),
        )

        while not self._stop.is_set():
            task = task_store.get_task(task_id)
            if not task or task["status"] != "active":
                checkpoint()
                return

            if messages is None:
                messages = task.get("messages") or []
                if not messages:
                    self._begin_task(task)
                    return

            if (
                rounds >= self.MAX_ROUNDS_PER_DRIVE
                or (time.monotonic() - drive_started) >= self.TASK_TICK_BUDGET_SECONDS
            ):
                checkpoint()
                task_store.update_task(task_id, next_run_at=datetime.now().isoformat())
                task_trace.record(
                    task_id, "yield", rounds=rounds,
                    reason="round cap" if rounds >= self.MAX_ROUNDS_PER_DRIVE else "time budget",
                )
                print(f"[task-runner] task {task_id} yielding after {rounds} round(s); resumes next tick")
                return

            probe_err = self._probe_ollama()
            if probe_err is not None:
                checkpoint()
                self._handle_runtime_error(task, probe_err)
                return

            self._emit(task_id, "thinking", round=rounds + 1)
            llm_started = time.monotonic()
            try:
                response = ollama.chat(
                    model=model,
                    messages=messages,
                    tools=tools,
                    stream=False,
                    deadline=self.STEP_LLM_DEADLINE,
                    options={"num_predict": self.AGENTIC_NUM_PREDICT},
                    priority="normal",
                )
            except Exception as e:
                checkpoint()
                task_trace.record(
                    task_id, "llm_error", round=rounds + 1,
                    latency_ms=round((time.monotonic() - llm_started) * 1000),
                    reason=task_trace.preview(str(e), 300),
                )
                self._handle_runtime_error(task, f"LLM error during round: {e}")
                return

            self._clear_runtime_retries(task_id, task)
            rounds += 1
            rounds_since_checkpoint += 1

            message = response.get("message", {})
            messages.append(message)
            tool_calls = message.get("tool_calls", []) or []
            task_trace.record(
                task_id, "round", round=rounds,
                latency_ms=round((time.monotonic() - llm_started) * 1000),
                tool_calls=len(tool_calls),
                text=task_trace.preview(message.get("content") or "", 300) if not tool_calls else "",
                **task_trace.usage_from_response(response),
            )

            if not tool_calls:
                text = (message.get("content") or "").strip()
                todos = task.get("plan") or []
                incomplete = [t for t in todos if str(t.get("status")) != "done"]
                # Completion requires an explicit finish_task (handled above) OR an
                # empty todo list. Trailing off with items still open is NOT done —
                # the model going silent (often an empty response when context gets
                # heavy) must never be mistaken for finishing the work.
                if not incomplete:
                    self._emit(task_id, "answer", detail=(text or "done")[:200])
                    task_store.save_session(task_id, messages)
                    self._finalize(task, "done", text or self._generate_report(task, "done"))
                    return

                if nudges >= self.MAX_NUDGES:
                    # Still stuck this drive. Don't lie about completion and don't
                    # spin: yield so a fresh drive retries next tick. Give up
                    # honestly only after repeated stuck cycles.
                    stuck = self._bump_stuck(task_id, task)
                    task_store.save_session(task_id, messages)
                    task_trace.record(
                        task_id, "stuck", cycle=stuck,
                        remaining=task_trace.preview("; ".join(t.get("description", "") for t in incomplete), 300),
                    )
                    if stuck >= self.MAX_STUCK_CYCLES:
                        remaining = "; ".join(t.get("description", "") for t in incomplete)
                        report = self._generate_report(task, "failed")
                        self._finalize(task, "failed", f"{report}\n\nNot completed: {remaining}")
                    else:
                        task_store.update_task(task_id, next_run_at=datetime.now().isoformat())
                    return

                nudges += 1
                remaining = "; ".join(t.get("description", "") for t in incomplete[:5])
                task_trace.record(task_id, "nudge", round=rounds, nudge=nudges,
                                  remaining=task_trace.preview(remaining, 300))
                messages.append({
                    "role": "user",
                    "content": (
                        f"You are NOT done — your todo list still has open items: {remaining}. "
                        "Do the next one now with your tools: write the actual files (e.g. the "
                        "landing page HTML/CSS), run/verify them, then mark the item done. When "
                        "everything is truly complete and verified, call finish_task. Don't stop "
                        "and don't just describe what you'd do — do it."
                    ),
                })
                task_store.save_session(task_id, messages)
                continue

            nudges = 0
            self._clear_stuck(task_id, task)
            control = None
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                inputs = fn.get("arguments", {}) or {}
                # Normalize stringified argument blobs once, centrally, so the
                # control tools below and the registry all see a dict (R-7).
                from tool_registry import ToolRegistry
                try:
                    inputs = ToolRegistry.normalize_inputs(inputs)
                except ValueError:
                    inputs = {}

                # Once the task is finishing or pausing, don't execute any more
                # calls from this batch — but still append a tool result for each
                # so the assistant's tool_calls stay paired (some models error on
                # a dangling tool_call with no result when the thread resumes).
                if control is not None:
                    messages.append({
                        "role": "tool", "name": name,
                        "content": json.dumps({"success": False, "skipped": "task is finishing or pausing"}),
                    })
                    task_trace.record(task_id, "tool_skipped", round=rounds, name=name,
                                      reason="task is finishing or pausing")
                    continue

                tool_started = time.monotonic()

                # Protected actions pause the task for a cross-surface approval
                # instead of being flatly refused (§6.3). A one-shot grant from
                # /approve lets the exact action through once.
                refusal = autonomous_tool_refusal(name, inputs)
                if refusal:
                    from core import task_approvals
                    if task_approvals.consume_grant(task, name, inputs):
                        self._emit(task_id, "tool", tool=name or "?",
                                   detail="running owner-approved action")
                        refusal = None
                if refusal:
                    messages.append({
                        "role": "tool", "name": name,
                        "content": json.dumps({
                            "success": False,
                            "waiting_for_approval": True,
                            "detail": (
                                "This action requires the owner's approval. The "
                                "task is pausing until they approve or deny it."
                            ),
                        }),
                    })
                    control = ("approval", name, inputs)
                    continue

                if name == "update_todos":
                    result = self._apply_todos(task_id, inputs)
                elif name == "set_workspace":
                    # Run it (sets the workspace for this drive) AND persist the
                    # new directory on the task, so the agent's re-homing survives
                    # tick boundaries and restarts instead of reverting to the
                    # workspace captured when the task was created.
                    result = self._run_tool(task_id, name, inputs)
                    new_cwd = (result.get("data") or {}).get("cwd") if result.get("success") else None
                    if new_cwd:
                        task_store.update_task(task_id, workspace_path=str(new_cwd))
                        self._emit(task_id, "tool", tool="set_workspace", detail=f"workspace → {new_cwd}"[:120])
                elif name == "wait_until":
                    reason, resume_at = self._parse_wait_request(inputs)
                    self._emit(task_id, "wait", detail=f"{reason} → resume {resume_at}"[:200])
                    messages.append({
                        "role": "tool", "name": name,
                        "content": json.dumps({"success": True, "waiting_until": resume_at, "reason": reason}),
                    })
                    control = ("wait", reason, resume_at)
                    continue
                elif name == "finish_task":
                    outcome = str(inputs.get("outcome") or "done").strip().lower()
                    if outcome not in {"done", "failed", "blocked"}:
                        outcome = "done"
                    report = str(inputs.get("report") or "").strip()
                    self._emit(task_id, "answer", detail=(report or outcome)[:200])
                    messages.append({
                        "role": "tool", "name": name,
                        "content": json.dumps({"success": True, "recorded": outcome}),
                    })
                    control = ("finish", outcome, report)
                    continue
                else:
                    result = self._run_tool(task_id, name, inputs)

                if not result.get("success", True):
                    self._emit(task_id, "tool_error", tool=name or "?",
                               detail=str(result.get("error") or "")[:200])
                stored = self._tool_content_for_thread(result, name)
                messages.append({"role": "tool", "name": name, "content": stored})
                self._trace_tool(task_id, rounds, name, inputs, result, stored, tool_started)

            # Checkpoint every N rounds, and always before a state change.
            checkpoint(force=control is not None)

            if control and control[0] == "approval":
                self._pause_for_approval(task, control[1], control[2])
                return
            if control and control[0] == "wait":
                self._schedule_wait(task, control[1], control[2])
                return
            if control and control[0] == "finish":
                self._finalize(task, control[1], control[2])
                return

            # Keep context bounded for long/multi-day tasks.
            before_compact = len(messages)
            messages, compacted = self._maybe_compact(messages, model)
            if compacted:
                task_trace.record(task_id, "compacted", round=rounds,
                                  before=before_compact, after=len(messages))
                checkpoint()
            # Loop immediately — drive to completion, no artificial gaps.

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    # Files-changed artifact for the completion handoff (§6.3): remember up
    # to this many distinct paths the task's tools touched.
    CHANGED_FILES_CAP = 50

    def _run_tool(self, task_id: int, name: str | None, inputs: dict) -> dict:
        # Protected-action gating happens in the drive loop (approval pause,
        # §6.3) before this is called.
        registry = self._get_registry()
        self._emit(task_id, "tool", tool=name or "?", detail=_summarize_tool_args(name, inputs))
        result = registry.execute(name, inputs)
        self._update_code_index_after_tool(name, inputs, result)
        self._record_changed_files(task_id, name, inputs, result)
        return result

    def _record_changed_files(self, task_id: int, name: str | None, inputs: dict, result: dict) -> None:
        """Accumulate the distinct file paths this task has modified in its
        constraints, so the completion handoff and task page can show what
        the task actually touched. Persisted → survives restarts."""
        from tools.code_intel.code_index import changed_paths_from_tool

        paths = changed_paths_from_tool(name or "", inputs, result)
        if not paths:
            return
        task = task_store.get_task(task_id)
        if not task:
            return
        constraints = dict(task.get("constraints") or {})
        seen = [str(p) for p in (constraints.get("_files_changed") or [])]
        overflow = int(constraints.get("_files_changed_overflow") or 0)
        for p in paths:
            p = str(p)
            if p not in seen:
                seen.append(p)
        if len(seen) > self.CHANGED_FILES_CAP:
            overflow += len(seen) - self.CHANGED_FILES_CAP
            seen = seen[: self.CHANGED_FILES_CAP]
        if overflow:
            constraints["_files_changed_overflow"] = overflow
        constraints["_files_changed"] = seen
        task_store.update_task(task_id, constraints=json.dumps(constraints))

    @staticmethod
    def _trace_tool(task_id: int, round_no: int, name: str | None, inputs: dict,
                    result: dict, stored: str, started: float) -> None:
        """One trace line per executed tool call: what ran, how long, whether
        it worked, and how much of the result the model actually got to see."""
        try:
            raw_chars = len(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            raw_chars = len(str(result))
        ok = bool(result.get("success", True)) if isinstance(result, dict) else True
        fields = {
            "round": round_no,
            "name": name,
            "args": task_trace.preview(inputs),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "success": ok,
            "result_chars": raw_chars,
            "stored_chars": len(stored),
            "trimmed": len(stored) < raw_chars,
        }
        if isinstance(result, dict) and result.get("error"):
            fields["error"] = task_trace.preview(result.get("error"), task_trace.RESULT_PREVIEW_CHARS)
        elif isinstance(result, dict):
            fields["result"] = task_trace.preview(result.get("data", result), task_trace.RESULT_PREVIEW_CHARS)
        task_trace.record(task_id, "tool", **fields)

    def _tool_content_for_thread(self, result: dict, tool_name: str | None = None) -> str:
        """Serialize a tool result for the persistent thread. Uses the same
        compaction rules as the interactive agent (D-3) with this runner's
        budget."""
        from core.history_compaction import compact_tool_result_for_history
        try:
            return compact_tool_result_for_history(
                tool_name, result, max_chars=self.MAX_TOOL_RESULT_CHARS
            )
        except Exception:
            return json.dumps({"success": bool(isinstance(result, dict) and result.get("success", True))})

    def _bump_stuck(self, task_id: int, task: dict) -> int:
        constraints = dict(task.get("constraints") or {})
        n = int(constraints.get("_stuck_count", 0)) + 1
        constraints["_stuck_count"] = n
        task_store.update_task(task_id, constraints=json.dumps(constraints))
        task["constraints"] = constraints
        return n

    def _clear_stuck(self, task_id: int, task: dict) -> None:
        constraints = task.get("constraints") or {}
        if constraints.get("_stuck_count"):
            new = dict(constraints)
            new.pop("_stuck_count", None)
            task_store.update_task(task_id, constraints=json.dumps(new))
            task["constraints"] = new

    def _apply_todos(self, task_id: int, inputs: dict) -> dict:
        raw = inputs.get("todos")
        if raw is None:
            raw = inputs.get("items") or []
        # Models sometimes pass the array as a JSON *string* rather than a real
        # array. Without this, `for item in raw` would iterate the string one
        # CHARACTER at a time and create a todo per letter. Coerce it back.
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = []
        if isinstance(raw, dict):
            raw = raw.get("todos") or raw.get("items") or [raw]
        if not isinstance(raw, list):
            raw = []

        todos: list[dict] = []
        for item in raw:
            if isinstance(item, dict):
                desc = str(item.get("description") or item.get("text") or "").strip()
                status = str(item.get("status") or "pending").strip().lower()
            elif isinstance(item, str):
                desc, status = item.strip(), "pending"
            else:
                continue
            if status not in {"pending", "in_progress", "done"}:
                status = "pending"
            if desc:
                todos.append({"description": desc, "status": status})
            if len(todos) >= 40:  # sane cap — guards against pathological input
                break
        done = sum(1 for t in todos if t["status"] == "done")
        task_store.update_task(task_id, plan=json.dumps(todos), current_step=done)
        task_store.append_history(task_id, {
            "type": "todos_updated",
            "todos": [{"description": t["description"], "status": t["status"]} for t in todos],
            "done": done,
            "total": len(todos),
        })
        return {"success": True, "todos": todos, "done": done, "total": len(todos)}

    # ------------------------------------------------------------------
    # Wait / blocked / finish handling
    # ------------------------------------------------------------------

    def _schedule_wait(self, task: dict, reason: str, resume_at: str) -> None:
        task_id = task["id"]
        constraints = dict(task.get("constraints") or {})
        wait_count = int(constraints.get("_wait_count", 0)) + 1
        if wait_count > self.MAX_WAITS:
            self._finalize(
                task, "blocked",
                f"Paused to wait {wait_count} times without finishing (last: {reason}).",
            )
            return
        constraints["_wait_count"] = wait_count
        constraints["_wait_reason"] = reason
        constraints["_wait_until"] = resume_at
        task_store.update_task(
            task_id, constraints=json.dumps(constraints), next_run_at=resume_at
        )
        task_store.append_history(task_id, {
            "type": "task_wait", "reason": reason, "resume_at": resume_at, "wait_count": wait_count,
        })
        task_trace.record(task_id, "wait", reason=task_trace.preview(reason, 300),
                          resume_at=resume_at, wait_count=wait_count)
        if wait_count == 1:
            self._notify(
                f"Task '{task['title']}' is waiting on something external before it "
                f"can continue: {reason}\n\nIt'll resume on its own around "
                f"{self._friendly_time(resume_at)}.",
                task.get("owner_chat_id"),
            )
        print(f"[task-runner] task {task_id} waiting until {resume_at}: {reason}")

    def _pause_for_approval(self, task: dict, tool: str, inputs: dict) -> None:
        """Block the task on a pending approval and ping the owner (§6.3)."""
        from core import task_approvals
        record = task_approvals.request_approval(task, tool, inputs)
        task_trace.record(task["id"], "approval_requested", tool=tool,
                          summary=task_trace.preview(record.get("summary"), 300))
        self._notify(
            f"Task #{task['id']} '{task['title']}' needs your approval to run "
            f"{tool}:\n\n    {record['summary']}\n\n"
            f"Reply /approve {task['id']} to allow it once, or /deny {task['id']} "
            f"to refuse.\n{self._task_link(task['id'])}",
            task.get("owner_chat_id"),
        )
        print(f"[task-runner] task {task['id']} blocked awaiting approval for {tool}")

    @staticmethod
    def _task_link(task_id: int) -> str:
        try:
            from core.web_auth import task_page_url
            return f"Details: {task_page_url(task_id)}"
        except Exception:
            return ""

    def _finalize(self, task: dict, outcome: str, report: str) -> None:
        task_id = task["id"]
        # Re-read: tools run since this round's load may have written
        # constraints (e.g. _files_changed) that a stale copy would clobber.
        task = task_store.get_task(task_id) or task
        # Clear any transient wait markers from constraints.
        constraints = {k: v for k, v in (task.get("constraints") or {}).items()
                       if not str(k).startswith("_wait")}
        task_store.update_task(task_id, constraints=json.dumps(constraints))

        if not report:
            report = self._generate_report(task, outcome)

        # Handoff artifact: what the task actually touched (§6.3).
        files_note = self._files_changed_note(constraints)
        if files_note:
            task_store.append_history(task_id, {"type": "files_changed", "detail": files_note})
        task_trace.record(
            task_id, "finished", outcome=outcome,
            report=task_trace.preview(report, task_trace.REPORT_PREVIEW_CHARS),
            files_changed=[str(p) for p in (constraints.get("_files_changed") or [])],
        )
        print(f"[task-runner] task {task_id} trace: {task_trace.trace_path(task_id)}")

        if outcome == "blocked":
            task_store.update_task(task_id, status="blocked", result=report)
            self._notify(
                f"Task blocked: {task['title']}\n\n{report}\n\n"
                "Reply with what you'd like me to do and I'll resume.\n"
                f"{self._task_link(task_id)}",
                task.get("owner_chat_id"),
            )
            print(f"[task-runner] task {task_id} blocked")
            return
        files_suffix = f"\n\n{files_note}" if files_note else ""
        if outcome == "failed":
            task_store.fail_task(task_id, report)
            self._notify(
                f"Task failed: {task['title']}\n\n{report}{files_suffix}\n{self._task_link(task_id)}",
                task.get("owner_chat_id"),
            )
            print(f"[task-runner] task {task_id} failed")
            return
        task_store.complete_task(task_id, report)
        self._notify(
            f"Task complete: {task['title']}\n\n{report}{files_suffix}\n{self._task_link(task_id)}",
            task.get("owner_chat_id"),
        )
        print(f"[task-runner] task {task_id} done")

    @staticmethod
    def _files_changed_note(constraints: dict) -> str:
        """One-line 'Files changed (N): a, b, c (+n more)' summary, or ''."""
        files = [str(p) for p in (constraints.get("_files_changed") or [])]
        if not files:
            return ""
        overflow = int(constraints.get("_files_changed_overflow") or 0)
        total = len(files) + overflow
        shown = files[:8]
        more = total - len(shown)
        listing = ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
        return f"Files changed ({total}): {listing}"

    def _parse_wait_request(self, inputs: dict) -> tuple[str, str]:
        reason = str(inputs.get("reason") or "waiting on external state").strip()[:300]
        now = datetime.now()
        resume_dt: datetime | None = None
        raw_at = inputs.get("resume_at")
        if raw_at:
            try:
                resume_dt = datetime.fromisoformat(str(raw_at).replace("Z", "").strip())
            except (ValueError, TypeError):
                resume_dt = None
        if resume_dt is None:
            mins = inputs.get("resume_in_minutes")
            try:
                minutes = float(mins) if mins is not None else 10.0
            except (ValueError, TypeError):
                minutes = 10.0
            resume_dt = now + timedelta(minutes=minutes)
        min_dt = now + timedelta(minutes=self.MIN_WAIT_MINUTES)
        max_dt = now + timedelta(minutes=self.MAX_WAIT_MINUTES)
        if resume_dt < min_dt:
            resume_dt = min_dt
        elif resume_dt > max_dt:
            resume_dt = max_dt
        return reason, resume_dt.isoformat()

    # ------------------------------------------------------------------
    # Runtime-error backoff (transient model/network failures)
    # ------------------------------------------------------------------

    def _handle_runtime_error(self, task: dict, error_text: str) -> None:
        task_id = task["id"]
        constraints = dict(task.get("constraints") or {})
        retries = int(constraints.get("_runtime_retries", 0)) + 1
        constraints["_runtime_retries"] = retries
        summary = str(error_text or "Runtime error")[:300]

        if retries > self.MAX_RUNTIME_RETRIES:
            result = (
                f"Task failed after {self.MAX_RUNTIME_RETRIES} retries because the "
                f"model/runtime was unavailable: {summary}"
            )
            task_store.fail_task(task_id, result)
            task_trace.record(task_id, "finished", outcome="failed", report=result, files_changed=[])
            self._notify(f"Task failed: {task['title']}\n\n{result}", task.get("owner_chat_id"))
            return

        backoff_secs = [30, 30, 60, 120, 300, 600, 900, 900, 900, 900, 900, 900]
        secs = backoff_secs[min(retries - 1, len(backoff_secs) - 1)]
        next_run = (datetime.now() + timedelta(seconds=secs)).isoformat()
        task_store.update_task(task_id, constraints=json.dumps(constraints), next_run_at=next_run)
        task_store.append_history(task_id, {
            "type": "step_retry", "attempt": retries, "reason": summary,
            "next_run_at": next_run, "backoff_seconds": secs,
        })
        task_trace.record(task_id, "runtime_error", attempt=retries, reason=summary,
                          backoff_seconds=secs)
        if retries in {1, 3, 6, 12}:
            wait_label = f"{secs}s" if secs < 60 else f"{secs // 60} minute(s)"
            self._notify(
                f"Temporary runtime issue on task '{task['title']}'; retrying in "
                f"{wait_label} instead of blocking.",
                task.get("owner_chat_id"),
            )

    def _clear_runtime_retries(self, task_id: int, task: dict) -> None:
        """Clear transient 'why am I paused' markers now that the task is making
        progress again — the runtime-retry counter and the waiting banner state.
        (The lifetime _wait_count cap is kept.)"""
        constraints = task.get("constraints") or {}
        if any(constraints.get(k) for k in ("_runtime_retries", "_wait_reason", "_wait_until")):
            new = dict(constraints)
            for k in ("_runtime_retries", "_wait_reason", "_wait_until"):
                new.pop(k, None)
            task_store.update_task(task_id, constraints=json.dumps(new))
            task["constraints"] = new

    def _probe_ollama(self) -> str | None:
        try:
            self._get_ollama().tags(request_timeout=2)
            return None
        except Exception as e:
            return f"Ollama probe failed: {e}"

    # ------------------------------------------------------------------
    # Reporting / finalize-on-deadline
    # ------------------------------------------------------------------

    def _generate_report(self, task: dict, outcome: str) -> str:
        plan = task.get("plan") or []
        history = task.get("history") or []
        todo_lines = "\n".join(
            f"- [{t.get('status', '?')}] {t.get('description', '')}" for t in plan
        ) if plan else "(no todo list recorded)"
        recent = []
        for h in history[-12:]:
            d = h.get("detail") or h.get("summary") or h.get("reason") or ""
            if d:
                recent.append(f"- {d}")
        summary = todo_lines + ("\n" + "\n".join(recent) if recent else "")
        prompt = _REPORT_PROMPT.format(
            goal=task.get("goal", ""), status=outcome, summary=summary[:4000]
        )
        try:
            ollama = self._get_ollama()
            cfg = self._model_config(task.get("owner_chat_id"))
            ollama.fallback_model = cfg.get("fallback_model")
            resp = ollama.chat(
                model=cfg["primary_model"],
                messages=[{"role": "user", "content": prompt}],
                stream=False, deadline=60,
                options={"num_predict": self.STRUCTURED_NUM_PREDICT},
                priority="normal",
            )
            text = (resp.get("message", {}).get("content") or "").strip()
            if text:
                return text
        except Exception as e:
            print(f"[task-runner] report generation failed: {e}")
        done = sum(1 for t in plan if t.get("status") == "done")
        return f"Task ended ({outcome}). Completed {done}/{len(plan)} planned items."

    def _finalize_overdue(self, task: dict) -> None:
        print(f"[task-runner] finalizing overdue task {task['id']}: {task['title']}")
        plan = task.get("plan") or []
        done = sum(1 for t in plan if t.get("status") == "done")
        # Reaching the deadline with real progress is a completion, not a failure.
        outcome = "failed" if (plan and done == 0) else "done"
        report = self._generate_report(task, "deadline reached")
        self._finalize(task, outcome, report)

    # ------------------------------------------------------------------
    # Context compaction
    # ------------------------------------------------------------------

    def _maybe_compact(self, messages: list, model: str) -> tuple[list, bool]:
        try:
            if not summarizer.needs_summarization(messages):
                return messages, False
            req = summarizer.build_summary_request(messages)
            if not req:
                return messages, False
            resp = self._get_ollama().chat(
                model=model, messages=req, stream=False, deadline=30,
                options={"num_predict": self.STRUCTURED_NUM_PREDICT},
                priority="normal",
            )
            summary_text = (resp.get("message", {}).get("content") or "").strip()
            if not summary_text:
                return messages, False
            return summarizer.apply_summary(messages, summary_text), True
        except Exception:
            # Hard fallback: keep system + a recent tail, dropping any leading
            # orphan tool messages so the thread stays valid.
            if len(messages) <= 24:
                return messages, False
            head = messages[:1] if messages and messages[0].get("role") == "system" else []
            tail = messages[-20:]
            while tail and tail[0].get("role") == "tool":
                tail = tail[1:]
            return head + tail, True

    # ------------------------------------------------------------------
    # Prompt / tool assembly
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        lumi_email = os.getenv("LUMI_EMAIL_ADDRESS", "").strip()
        identity_parts = []
        if lumi_email:
            identity_parts.append(f"Your email address: {lumi_email}")
        identity_file = get_data_dir() / "identity" / "identity.txt"
        if identity_file.exists():
            identity_parts.append(
                f"Your identity file (existing accounts & credentials): {identity_file} — "
                "read it before signing up for anything new, and append new accounts after creating them."
            )
        identity_block = "\n".join(identity_parts) + "\n\n" if identity_parts else ""

        return (
            "You are Lumi, working fully autonomously to complete a goal from start "
            "to finish, the way a sharp, senior engineer would. No human is watching "
            "each step — you decide what to do and you do it with your tools.\n\n"
            "How to work:\n"
            "- LOOK BEFORE YOU ACT. Inspect the workspace first (list files, read "
            "what's relevant). Build on what already exists — if a needed file or "
            "result is already there and valid, reuse it instead of regenerating it. "
            "Never blindly redo work that's already done.\n"
            "- Keep a live todo list with the update_todos tool. Lay it out after you've "
            "looked around, then keep it current: mark items in_progress and done as you "
            "go. The user watches this list.\n"
            "- Work adaptively: take an action, observe the real result, decide the next "
            "action from what actually happened — don't follow a frozen script.\n"
            "- Verify your own work. Read back files you wrote, run the code, check the "
            "output is actually correct before moving on.\n"
            "- Act, don't narrate. Prefer calling a tool over writing about what you would do.\n"
            "- For project overview/build/test/framework questions, call inspect_project. "
            "For code, prefer find_definition / search_symbols / read_symbol. For git "
            "status/diff/log use the git_* tools, not raw git via run_command.\n\n"
            f"Current working directory: {get_repo_root()}\n"
            "Relative paths AND shell commands (execute_shell/run_command) run from this "
            "directory. If the goal is about building or working inside a specific folder, "
            "call set_workspace to point at that folder FIRST — then your scripts, shell "
            "commands, and relative paths all land in the right place and you won't have to "
            "prefix everything with absolute paths or 'cd'. Don't write project files inside "
            "the LumaKit application directory unless that's explicitly the working directory.\n\n"
            f"{identity_block}"
            "Finishing and waiting:\n"
            "- When the ENTIRE goal is accomplished (and you've verified it), call "
            "finish_task with outcome='done' and a clear report for the user.\n"
            "- If you genuinely cannot proceed without a human (login, 2FA, payment, an "
            "owner-only decision), call finish_task with outcome='blocked' and explain.\n"
            "- If — and only if — you must wait for something external before you can "
            "continue (a build/deploy, an email reply, a time/market window, a rate-limit "
            "cooldown), call wait_until with a reason and resume_in_minutes. The task "
            "sleeps and resumes automatically. Never use wait_until just to pace yourself."
        )

    def _build_tool_list(self, registry) -> list[dict]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": registry.get(t["name"])["inputSchema"],
                },
            }
            for t in registry.list()
        ]
        tools.append(self._todos_tool_schema())
        tools.append(self._wait_tool_schema())
        tools.append(self._finish_tool_schema())
        return tools

    @staticmethod
    def _todos_tool_schema() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "update_todos",
                "description": (
                    "Maintain your live TODO list for this task. Pass the FULL list "
                    "each time — it replaces the previous one. Mark items in_progress "
                    "and done as you work. The user sees this list, so keep it accurate."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "description": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "done"],
                                    },
                                },
                                "required": ["description", "status"],
                            },
                        }
                    },
                    "required": ["todos"],
                },
            },
        }

    @staticmethod
    def _wait_tool_schema() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "wait_until",
                "description": (
                    "Pause the task and resume later, ONLY when you must wait for "
                    "something external (a build/deploy, an email/reply, a time or "
                    "market window, a rate-limit cooldown). The task sleeps and resumes "
                    "automatically. Never use this to pace your own work."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "What you are waiting for."},
                        "resume_in_minutes": {"type": "number", "description": "Minutes to wait before resuming."},
                        "resume_at": {"type": "string", "description": "Optional absolute ISO 8601 resume time."},
                    },
                    "required": ["reason"],
                },
            },
        }

    @staticmethod
    def _finish_tool_schema() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "finish_task",
                "description": (
                    "Call this ONCE when the entire goal is fully accomplished (and "
                    "verified), or when it truly cannot be done. Provide the outcome and "
                    "a clear final report for the user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "outcome": {
                            "type": "string",
                            "enum": ["done", "failed", "blocked"],
                            "description": "done = goal achieved; failed = couldn't be done; blocked = needs a human (login/2FA/payment/owner decision).",
                        },
                        "report": {
                            "type": "string",
                            "description": "Final report for the user: what was accomplished, key results, anything left and why.",
                        },
                    },
                    "required": ["outcome", "report"],
                },
            },
        }

    # ------------------------------------------------------------------
    # Live activity feed
    # ------------------------------------------------------------------

    def _emit(self, task_id: int, kind: str, **extra) -> None:
        try:
            task_store.append_history(task_id, {"type": "activity", "kind": kind, **extra})
        except Exception as exc:
            # Keep the task alive, but a failing history write means live
            # activity is being lost — make that visible.
            from core import log
            log.warn("task_runner", f"history write failed for task {task_id} ({kind})", exc)

    # ------------------------------------------------------------------
    # Model / registry / workspace helpers
    # ------------------------------------------------------------------

    def _model_config(self, owner_chat_id: str | None = None) -> dict:
        from core.providers import default_fallback_model, default_model
        cfg = get_effective_config_for_user(owner_chat_id)
        return {
            "primary_model": cfg.get("primary_model") or default_model(),
            "fallback_model": cfg.get("fallback_model") or default_fallback_model(),
        }

    def _get_ollama(self):
        # Rebuild when provider settings changed (Settings saves apply to the
        # next drive round — no backend restart needed).
        from core.providers import create_llm_client, default_fallback_model, provider_fingerprint

        fingerprint = provider_fingerprint()
        if self._ollama is None or self._llm_fingerprint != fingerprint:
            self._ollama = create_llm_client(fallback_model=default_fallback_model() or None)
            self._llm_fingerprint = fingerprint
        return self._ollama

    def _get_registry(self):
        root = get_repo_root().resolve(strict=False)
        if self._registry is None or self._registry_root != root:
            from tool_registry import ToolRegistry
            from tools.code_intel.code_index import LazyCodeIndex

            self._registry = ToolRegistry()
            self._registry.load_tools_from_folder(skip_dirs={"code_intel"})
            self._code_index = LazyCodeIndex(root=root)
            for tool in self._code_index.get_tools():
                self._registry.register(tool, group="code_intel")
            self._registry_root = root
        return self._registry

    def _resolve_task_workspace(self, task: dict) -> Path | None:
        workspace = task.get("workspace_path")
        if not workspace:
            return None
        path = Path(workspace).expanduser().resolve(strict=False)
        if path.exists() and path.is_dir():
            return path
        return None

    def _resolve_task_workspace_or_fallback(self, task: dict) -> Path:
        resolved = self._resolve_task_workspace(task)
        if resolved is not None:
            return resolved
        fallback = get_repo_root()
        task_id = task["id"]
        recorded = task.get("workspace_path") or "none recorded"
        already_warned = any(
            isinstance(h, dict) and h.get("type") == "workspace_fallback"
            for h in (task.get("history") or [])
        )
        if not already_warned:
            print(f"[task-runner] task {task_id} workspace missing ({recorded}); falling back to {fallback}")
            task_store.append_history(task_id, {
                "type": "workspace_fallback", "recorded": str(recorded), "fallback": str(fallback),
            })
            self._notify(
                f"Task workspace missing for: {task['title']}\n\n"
                f"Recorded: {recorded}\nRunning in fallback: {fallback}",
                task.get("owner_chat_id"),
            )
        return fallback

    def _update_code_index_after_tool(self, name: str, inputs: dict, result: dict) -> None:
        from tools.code_intel.code_index import update_index_after_tool

        update_index_after_tool(self._code_index, name, inputs, result)

    @staticmethod
    def _friendly_time(iso: str) -> str:
        try:
            return datetime.fromisoformat(iso).strftime("%b %d %H:%M")
        except (ValueError, TypeError):
            return iso
