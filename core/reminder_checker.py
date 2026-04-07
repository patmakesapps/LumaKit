from __future__ import annotations

import threading
from core import memory_store
from core.cli import _c, CYAN, YELLOW, BOLD, RESET


def _default_notify(reminder: dict) -> None:
    """Print the reminder to the terminal. Swap this out for iMessage/email/push later."""
    print(f"\n{_c(CYAN, 'Lumi')} {_c(YELLOW, '🔔 Reminder:')} {_c(BOLD, reminder['content'])}")


class ReminderChecker:
    """Background thread that checks for due reminders every interval."""

    def __init__(self, interval: int = 60, notify=None):
        self._interval = interval
        self._notify = notify or _default_notify
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _check(self) -> None:
        while not self._stop.is_set():
            try:
                due = memory_store.get_due_reminders()
                for reminder in due:
                    self._notify(reminder)
                    memory_store.delete(reminder["id"])
            except Exception:
                pass  # don't crash the background thread
            self._stop.wait(self._interval)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._check, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
