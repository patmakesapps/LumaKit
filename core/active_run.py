"""Active run control and mid-run user input helpers."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
from typing import Callable


@dataclass
class RunActivity:
    at: float
    kind: str
    text: str
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "at": self.at,
            "kind": self.kind,
            "text": self.text,
            "meta": dict(self.meta),
        }


def _preview_text(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class ActiveRunController:
    """Thread-safe state for one active agent run."""

    ACTIVE_STATES = {"running", "awaiting_confirm"}

    def __init__(self, history_limit: int = 20):
        self._lock = RLock()
        self._history_limit = max(5, int(history_limit))
        self._next_run_id = 0
        self._recent_activity: deque[RunActivity] = deque(maxlen=self._history_limit)
        self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        self._run_id = 0
        self._state = "idle"
        self._kind = "chat"
        self._prompt_preview = ""
        self._stop_requested = False
        self._pending_guidance: list[str] = []
        self._current_phase = None
        self._current_round = None
        self._current_tool = None
        self._current_confirm_prompt = None
        self._started_at = None
        self._finished_at = None
        self._last_activity_at = None
        self._last_status_at = None
        self._last_model_activity_at = None
        self._last_tool_activity_at = None
        self._last_error = None

    def start_run(self, prompt: str = "", *, kind: str = "chat") -> int:
        with self._lock:
            self._next_run_id += 1
            self._recent_activity.clear()
            self._reset_unlocked()
            self._run_id = self._next_run_id
            self._state = "running"
            self._kind = kind
            self._prompt_preview = _preview_text(prompt, limit=280)
            now = time.time()
            self._started_at = now
            self._last_activity_at = now
            return self._run_id

    def finish_run(self, state: str, *, final_message: str = "", error: str = "") -> None:
        with self._lock:
            if self._run_id == 0:
                return
            self._state = state
            self._finished_at = time.time()
            self._current_phase = None
            self._current_round = None
            self._current_tool = None
            self._current_confirm_prompt = None
            if error:
                self._last_error = error
                self.note_activity(
                    "error",
                    _preview_text(error, limit=280),
                    meta={"final": True},
                )
            elif final_message:
                self.note_activity(
                    "final",
                    _preview_text(final_message, limit=280),
                    meta={"final": True},
                )

    def request_stop(self, reason: str = "Stop requested by the user.") -> bool:
        with self._lock:
            if self._state not in self.ACTIVE_STATES:
                return False
            if self._stop_requested:
                return False
            self._stop_requested = True
            self.note_activity("control", reason, meta={"stop_requested": True})
            return True

    def is_interrupted(self) -> bool:
        with self._lock:
            return self._stop_requested

    def is_running(self) -> bool:
        with self._lock:
            return self._state in self.ACTIVE_STATES

    def submit_guidance(self, text: str) -> bool:
        guidance = _preview_text(text, limit=600)
        if not guidance:
            return False
        with self._lock:
            if self._state not in self.ACTIVE_STATES:
                return False
            self._pending_guidance.append(guidance)
            self.note_activity("guidance", f"Queued guidance: {guidance}")
            return True

    def consume_pending_guidance(self) -> list[str]:
        with self._lock:
            if not self._pending_guidance:
                return []
            pending = self._pending_guidance[:]
            self._pending_guidance.clear()
            return pending

    def mark_model_round_start(self, round_num: int) -> None:
        with self._lock:
            self._state = "running"
            self._current_phase = "model"
            self._current_round = round_num
            self._current_tool = None
            self._last_model_activity_at = time.time()

    def mark_model_round_end(self, round_num: int | None = None) -> None:
        with self._lock:
            self._current_phase = "model"
            self._current_round = round_num
            self._last_model_activity_at = time.time()

    def mark_tool_start(self, tool_name: str, detail: str = "") -> None:
        with self._lock:
            self._state = "running"
            self._current_phase = "tool"
            self._current_tool = tool_name
            self._last_tool_activity_at = time.time()
            text = detail or f"Using {tool_name}."
            self.note_activity(
                "tool",
                _preview_text(text, limit=220),
                meta={"tool_name": tool_name},
            )

    def mark_tool_end(self, tool_name: str, summary: str = "", *, error: bool = False) -> None:
        with self._lock:
            self._current_phase = "tool"
            self._current_tool = tool_name
            self._last_tool_activity_at = time.time()
            if summary:
                self.note_activity(
                    "error" if error else "tool_result",
                    _preview_text(summary, limit=280),
                    meta={"tool_name": tool_name},
                )

    def mark_confirm_waiting(self, prompt: str) -> None:
        with self._lock:
            self._state = "awaiting_confirm"
            self._current_confirm_prompt = _preview_text(prompt, limit=220)
            self.note_activity("confirm", self._current_confirm_prompt)

    def clear_confirm_waiting(self) -> None:
        with self._lock:
            if self._state == "awaiting_confirm":
                self._state = "running"
            self._current_confirm_prompt = None

    def note_activity(self, kind: str, text: str, *, meta: dict | None = None) -> None:
        clean = _preview_text(text, limit=400)
        if not clean:
            return
        now = time.time()
        with self._lock:
            self._last_activity_at = now
            if kind == "status":
                self._last_status_at = now
            self._recent_activity.append(RunActivity(now, kind, clean, meta or {}))

    def get_status_snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "run_id": self._run_id,
                "active": self._state in self.ACTIVE_STATES,
                "state": self._state,
                "kind": self._kind,
                "prompt_preview": self._prompt_preview,
                "stop_requested": self._stop_requested,
                "pending_guidance_count": len(self._pending_guidance),
                "current_phase": self._current_phase,
                "current_round": self._current_round,
                "current_tool": self._current_tool,
                "current_confirm_prompt": self._current_confirm_prompt,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "last_activity_at": self._last_activity_at,
                "last_status_at": self._last_status_at,
                "last_model_activity_at": self._last_model_activity_at,
                "last_tool_activity_at": self._last_tool_activity_at,
                "last_error": self._last_error,
                "elapsed_seconds": (
                    round(now - self._started_at, 1) if self._started_at else 0.0
                ),
                "idle_for_seconds": (
                    round(now - self._last_activity_at, 1)
                    if self._last_activity_at
                    else None
                ),
                "recent_activity": [entry.as_dict() for entry in self._recent_activity],
            }


class StallWatchdog:
    """Background thread that emits a single 'still working' notice per stall.

    The controller already records timestamps for model / tool / activity. This
    just polls the snapshot and calls the caller's notice hook when a soft
    threshold is crossed. The hook fires once per stall episode — the counters
    reset once fresh activity comes in.
    """

    def __init__(
        self,
        controller: "ActiveRunController",
        notify: Callable[[str], None],
        *,
        model_threshold_s: float = 35.0,
        tool_threshold_s: float = 45.0,
        idle_threshold_s: float = 60.0,
        poll_interval_s: float = 5.0,
    ):
        self._controller = controller
        self._notify = notify
        self._model_threshold = model_threshold_s
        self._tool_threshold = tool_threshold_s
        self._idle_threshold = idle_threshold_s
        self._poll_interval = max(1.0, poll_interval_s)
        self._stop = Event()
        self._thread: Thread | None = None
        self._last_fired_key: tuple | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_fired_key = None
        self._thread = Thread(target=self._run, name="lumakit-stall-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self._tick()
            except Exception:
                # Watchdogs must not take the run down.
                pass

    def _tick(self) -> None:
        snap = self._controller.get_status_snapshot()
        if not snap.get("active"):
            return
        if snap.get("state") == "awaiting_confirm":
            return
        now = time.time()
        phase = snap.get("current_phase")
        last_tool = snap.get("last_tool_activity_at")
        last_model = snap.get("last_model_activity_at")
        last_activity = snap.get("last_activity_at")
        current_tool = snap.get("current_tool")

        notice: str | None = None
        key: tuple | None = None

        if phase == "tool" and last_tool and (now - last_tool) >= self._tool_threshold:
            elapsed = int(now - last_tool)
            key = ("tool", current_tool, elapsed // max(1, int(self._tool_threshold)))
            notice = (
                f"Still working — `{current_tool or 'current step'}` has been "
                f"running for {elapsed}s. Send stop if you want me to bail."
            )
        elif phase == "model" and last_model and (now - last_model) >= self._model_threshold:
            elapsed = int(now - last_model)
            key = ("model", elapsed // max(1, int(self._model_threshold)))
            notice = (
                f"Still thinking — the model has been deciding for {elapsed}s. "
                "You can send guidance or stop while I wait."
            )
        elif last_activity and (now - last_activity) >= self._idle_threshold:
            elapsed = int(now - last_activity)
            key = ("idle", elapsed // max(1, int(self._idle_threshold)))
            notice = (
                f"Nothing has moved in {elapsed}s. I'll keep waiting, but you "
                "can stop or nudge me with guidance."
            )

        if notice and key != self._last_fired_key:
            self._last_fired_key = key
            self._notify(notice)


