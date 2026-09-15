"""Per-task execution traces for real-user testing.

Every autonomous task writes one JSONL file under ``~/.lumakit/traces/`` —
one line per model round, tool call, pause, retry, and the final outcome.
Nothing large is stored: tool arguments and results are kept as short
previews plus their sizes, so a 50-round task is tens of kilobytes.

The task history table already holds the live activity feed for the UI;
this is the flat, readable record you open after a test run to see where
the agent went wrong, and can keep as a regression case.

Env:
  LUMAKIT_TASK_TRACES=0            disable tracing (default: on)
  LUMAKIT_TRACE_RETENTION_DAYS=30  prune traces older than this on runner start
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from core import log
from core.paths import get_data_dir

TRACES_DIRNAME = "traces"
ARG_PREVIEW_CHARS = 400
RESULT_PREVIEW_CHARS = 300
REPORT_PREVIEW_CHARS = 2000
DEFAULT_RETENTION_DAYS = 30


def enabled() -> bool:
    return os.getenv("LUMAKIT_TASK_TRACES", "1").strip().lower() not in {"0", "false", "off", "no"}


def traces_dir() -> Path:
    path = get_data_dir() / TRACES_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def trace_path(task_id: int) -> Path:
    return traces_dir() / f"task-{int(task_id)}.jsonl"


def preview(value, limit: int = ARG_PREVIEW_CHARS) -> str:
    """Short, single-line, JSON-ish preview of any value."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + f"... [+{len(text) - limit} chars]"
    return text


def usage_from_response(response: dict) -> dict:
    """Token counts if the provider reported them, normalized to
    prompt/completion. Ollama, Anthropic, and OpenAI-compatible shapes."""
    if not isinstance(response, dict):
        return {}
    usage = response.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
        completion = usage.get("output_tokens", usage.get("completion_tokens"))
    else:
        prompt = response.get("prompt_eval_count")
        completion = response.get("eval_count")
    out = {}
    if isinstance(prompt, int):
        out["prompt_tokens"] = prompt
    if isinstance(completion, int):
        out["completion_tokens"] = completion
    return out


def reset(task_id: int) -> None:
    """Start a task's trace from scratch. Task ids never repeat within one
    database, so an existing file means a stale database or a test run."""
    if not enabled():
        return
    try:
        trace_path(task_id).unlink(missing_ok=True)
    except Exception as exc:
        log.warn("task_trace", f"could not reset trace for task {task_id}", exc)


def record(task_id: int, event: str, **fields) -> None:
    """Append one event line. Never raises — a trace must not break a task."""
    if not enabled():
        return
    entry = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event}
    entry.update(fields)
    try:
        with trace_path(task_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        log.warn("task_trace", f"could not write trace for task {task_id}", exc)


def prune(days: int | None = None) -> int:
    """Delete trace files older than *days* (by modification time). Returns
    the number removed."""
    if days is None:
        try:
            days = int(os.getenv("LUMAKIT_TRACE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
        except ValueError:
            days = DEFAULT_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        for path in traces_dir().glob("task-*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception as exc:
        log.warn("task_trace", "trace prune failed", exc)
    return removed


def read(task_id: int) -> list[dict]:
    path = trace_path(task_id)
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"event": "unparseable", "raw": line[:200]})
    return entries


def list_traces() -> list[dict]:
    """Newest first: task id, file size, last write, and outcome if finished."""
    rows = []
    for path in traces_dir().glob("task-*.jsonl"):
        try:
            task_id = int(path.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        stat = path.stat()
        title = ""
        outcome = ""
        for entry in read(task_id):
            if entry.get("event") == "task_started":
                title = str(entry.get("title") or "")
            elif entry.get("event") == "finished":
                outcome = str(entry.get("outcome") or "")
        rows.append({
            "task_id": task_id,
            "title": title,
            "outcome": outcome or "in progress",
            "bytes": stat.st_size,
            "updated": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    rows.sort(key=lambda r: r["task_id"], reverse=True)
    return rows


def _fmt_ms(ms) -> str:
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return "-"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


def _fmt_k(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def render(task_id: int) -> str:
    """Human-readable rendering of one trace for the terminal."""
    entries = read(task_id)
    if not entries:
        return f"No trace for task {task_id} (looked in {trace_path(task_id)})."

    lines: list[str] = []
    tool_counts: Counter = Counter()
    tool_failures: Counter = Counter()
    llm_ms = 0.0
    tool_ms = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    rounds = 0
    trimmed = 0
    nudges = 0
    errors = 0
    outcome = None

    for e in entries:
        ev = e.get("event")
        ts = str(e.get("ts", ""))[11:19]
        if ev == "task_started":
            lines.append(f"Task {task_id}: {e.get('title', '')}")
            lines.append(f"  goal:      {e.get('goal', '')}")
            lines.append(f"  workspace: {e.get('workspace', '')}")
            lines.append(f"  started:   {e.get('ts', '')}")
        elif ev == "drive_started":
            lines.append(
                f"{ts}  drive  model={e.get('model')} fallback={e.get('fallback') or '-'} "
                f"tools={e.get('tools')}"
            )
        elif ev == "round":
            rounds += 1
            llm_ms += float(e.get("latency_ms") or 0)
            prompt_tokens += int(e.get("prompt_tokens") or 0)
            completion_tokens += int(e.get("completion_tokens") or 0)
            calls = e.get("tool_calls") or 0
            what = f"{calls} tool call(s)" if calls else f"text: {e.get('text', '')}"
            tok = ""
            if e.get("prompt_tokens") is not None:
                tok = f" tokens={e.get('prompt_tokens')}+{e.get('completion_tokens') or 0}"
            lines.append(f"{ts}  #{e.get('round')}  llm {_fmt_ms(e.get('latency_ms'))}{tok}  {what}")
        elif ev == "tool":
            name = str(e.get("name") or "?")
            tool_counts[name] += 1
            tool_ms += float(e.get("duration_ms") or 0)
            ok = e.get("success")
            if ok is False:
                tool_failures[name] += 1
            status = "ok" if ok else "FAIL"
            size = f"stored {_fmt_k(e.get('stored_chars'))}/{_fmt_k(e.get('result_chars'))}"
            if e.get("trimmed"):
                trimmed += 1
                size += " TRIMMED"
            lines.append(
                f"{ts}       {name} {_fmt_ms(e.get('duration_ms'))} {status}  {size}"
            )
            lines.append(f"             args: {e.get('args', '')}")
            if e.get("error"):
                lines.append(f"             error: {e.get('error')}")
            elif e.get("result"):
                lines.append(f"             result: {e.get('result')}")
        elif ev == "tool_skipped":
            lines.append(f"{ts}       {e.get('name')} skipped ({e.get('reason')})")
        elif ev == "nudge":
            nudges += 1
            lines.append(f"{ts}       NUDGE #{e.get('nudge')} open todos: {e.get('remaining', '')}")
        elif ev == "approval_requested":
            lines.append(f"{ts}       PAUSED for approval: {e.get('tool')} — {e.get('summary', '')}")
        elif ev == "wait":
            lines.append(f"{ts}       WAIT until {e.get('resume_at')}: {e.get('reason', '')}")
        elif ev == "runtime_error":
            errors += 1
            lines.append(f"{ts}       RUNTIME ERROR (attempt {e.get('attempt')}): {e.get('reason', '')}")
        elif ev == "llm_error":
            errors += 1
            lines.append(f"{ts}       LLM ERROR: {e.get('reason', '')}")
        elif ev == "compacted":
            lines.append(f"{ts}       context compacted ({e.get('before')} → {e.get('after')} messages)")
        elif ev == "yield":
            lines.append(f"{ts}       yield after {e.get('rounds')} round(s): {e.get('reason', '')}")
        elif ev == "stuck":
            lines.append(f"{ts}       STUCK cycle {e.get('cycle')} — {e.get('remaining', '')}")
        elif ev == "finished":
            outcome = e.get("outcome")
            lines.append(f"{ts}  FINISHED: {str(outcome).upper()}")
            lines.append(f"  report: {e.get('report', '')}")
            files = e.get("files_changed") or []
            if files:
                lines.append(f"  files changed ({len(files)}): {', '.join(files)}")
        else:
            lines.append(f"{ts}       {ev}: {preview({k: v for k, v in e.items() if k not in {'ts', 'event'}}, 200)}")

    lines.append("")
    lines.append("Summary")
    lines.append(f"  outcome:       {outcome or 'in progress'}")
    lines.append(f"  rounds:        {rounds}  (llm time {_fmt_ms(llm_ms)})")
    tools_total = sum(tool_counts.values())
    lines.append(f"  tool calls:    {tools_total}  (tool time {_fmt_ms(tool_ms)}, {trimmed} trimmed result(s))")
    for name, n in tool_counts.most_common():
        fail = f", {tool_failures[name]} failed" if tool_failures[name] else ""
        lines.append(f"    {name}: {n}{fail}")
    if prompt_tokens or completion_tokens:
        lines.append(f"  tokens:        {prompt_tokens} prompt + {completion_tokens} completion")
    lines.append(f"  nudges/errors: {nudges} / {errors}")
    lines.append(f"  file:          {trace_path(task_id)}")
    return "\n".join(lines)
