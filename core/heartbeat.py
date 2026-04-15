"""Heartbeat — gives Lumi a pulse so it can reach out on its own."""

import os
import threading
import time
from datetime import datetime

from core import memory_store
from ollama_client import OllamaClient


class Heartbeat:
    def __init__(self, send, interval=900, cooldown=3600, inject_session=None, owner_chat_id=None):
        """
        send           — callable(text) to message the user
        interval       — seconds between checks (default 15 min)
        cooldown       — minimum seconds between outbound messages (default 1 hr)
        inject_session — optional callable(text) that appends an assistant message
                         to the owner's agent session so follow-up replies have context
        owner_chat_id  — the chat_id the heartbeat targets. Used to scope memory
                         lookups so other users' memories don't bleed in. None means
                         "no scope filter" (legacy/CLI behavior).
        """
        self._send = send
        self._interval = interval
        self._cooldown = cooldown
        self._inject = inject_session
        self._owner_chat_id = str(owner_chat_id) if owner_chat_id is not None else None
        self._stop = threading.Event()
        self._thread = None
        self._last_sent = 0
        # Store last context + whether we reached out, so the LLM can avoid repeats
        self._last_context = ""
        self._last_reached_out = False

    def _build_context(self):
        now = datetime.now()
        time_str = now.strftime("%A %I:%M %p")
        now_iso = now.isoformat()

        # Recent memories (last 10), scoped to the owner so other users' memories don't bleed in.
        # Skip reminders that haven't fired yet — ReminderChecker will deliver them automatically
        # at notify_at, so surfacing them here just tempts the LLM to pre-nag the user.
        recent = memory_store.get_recent(10, active_user=self._owner_chat_id)
        memory_lines = []
        for m in recent:
            if m["type"] == "reminder" and m.get("notify_at") and m["notify_at"] > now_iso:
                continue
            line = f"- [{m['type']}] {m['content']}"
            if m.get("notify_at"):
                line += f" (notify: {m['notify_at']})"
            memory_lines.append(line)
        memories = "\n".join(memory_lines) if memory_lines else "None"

        # Time since last conversation
        minutes_quiet = int((time.time() - self._last_sent) / 60) if self._last_sent else None
        quiet_str = f"{minutes_quiet} minutes ago" if minutes_quiet else "No messages sent yet this session"

        context = (
            f"Time: {time_str}\n"
            f"Last message to user: {quiet_str}\n"
            f"Recent memories:\n{memories}"
        )
        return context

    def _pulse(self):
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break

            # Cooldown check
            if self._last_sent and (time.time() - self._last_sent) < self._cooldown:
                continue

            context = self._build_context()

            prompt = (
                "You are Lumi. You're checking in on the user.\n\n"
                f"Current context:\n{context}\n\n"
                f"Last time you checked, this was the context:\n{self._last_context or 'First check'}\n"
                f"Did you message the user last time? {'Yes' if self._last_reached_out else 'No'}\n\n"
                "Should you message the user right now? Only if you have a genuine reason — "
                "following up on something, a timely observation, or something useful. "
                "Do NOT repeat yourself or say the same thing as last time. "
                "Do NOT message just to say hi with nothing to add.\n\n"
                "If yes, write the message (keep it short and natural, use slang). "
                "If no, respond with exactly: NONE"
            )

            try:
                client = OllamaClient()
                model = os.getenv("OLLAMA_MODEL")
                response = client.chat(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    stream=False,
                    deadline=30,
                )
                reply = response.get("message", {}).get("content", "").strip()

                self._last_context = context

                if reply and reply.upper() != "NONE":
                    self._send(reply)
                    if self._inject:
                        try:
                            self._inject(reply)
                        except Exception:
                            pass
                    self._last_sent = time.time()
                    self._last_reached_out = True
                else:
                    self._last_reached_out = False

            except Exception:
                pass  # don't crash the background thread

    def notify_activity(self):
        """Call this when the user sends a message, so cooldown resets."""
        self._last_sent = time.time()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._pulse, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
