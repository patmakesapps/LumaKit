"""Core service — owns long-lived background workers and the notification router.

Surfaces (CLI, Telegram, web, future connectors) register with the service's
NotificationRouter. Reminders, tasks, heartbeat, and email all route outbound
messages through that single registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core import auth, notifications
from core.email_checker import EmailChecker
from core.heartbeat import Heartbeat
from core.identity import chat_owner_id
from core.reminder_checker import ReminderChecker
from core.runtime_config import get_effective_config_for_user
from core.task_runner import TaskRunner
from ollama_client import OllamaClient


@dataclass
class Surface:
    """A registered outbound channel.

    name           — unique label ("telegram", "web", ...).
    deliver        — deliver(payload) -> bool. Returns True if actually sent.
    inject_session — optional: append an assistant-message to this surface's
                     active session (used by heartbeat/email so follow-ups land
                     in the right context).
    is_owner       — True if this surface represents the owner (heartbeat +
                     email route here).
    """

    name: str
    deliver: Callable[[dict], bool]
    inject_session: Callable[[str], None] | None = None
    is_owner: bool = False


class NotificationRouter:
    """Picks which registered surface(s) receive a payload.

    Payload fields:
      content     — the message text (required)
      label       — human label ("Reminder", "Task", ...)
      target      — "auto" | "both" | <surface name>. Default "auto".
      chat_id     — optional surface-native target hint
    """

    def __init__(self):
        self._surfaces: dict[str, Surface] = {}

    def register(self, surface: Surface) -> None:
        self._surfaces[surface.name] = surface

    def unregister(self, name: str) -> None:
        self._surfaces.pop(name, None)

    def owner_surface(self) -> Surface | None:
        for s in self._surfaces.values():
            if s.is_owner:
                return s
        return next(iter(self._surfaces.values()), None)

    def route(self, payload: dict) -> bool:
        target = payload.get("target", "auto")
        if target == "both":
            delivered = False
            for s in self._surfaces.values():
                try:
                    if s.deliver(payload):
                        delivered = True
                except Exception:
                    pass
            return delivered
        if target in self._surfaces:
            try:
                return bool(self._surfaces[target].deliver(payload))
            except Exception:
                return False
        # auto: owner surface first, then any other
        owner = self.owner_surface()
        if owner:
            try:
                if owner.deliver(payload):
                    return True
            except Exception:
                pass
        for s in self._surfaces.values():
            if s is owner:
                continue
            try:
                if s.deliver(payload):
                    return True
            except Exception:
                pass
        return False

    def notify_owner(self, content: str, label: str = "") -> bool:
        owner = self.owner_surface()
        if not owner:
            return False
        try:
            return bool(owner.deliver({"content": content, "label": label}))
        except Exception:
            return False

    def inject_owner_session(self, text: str) -> None:
        owner = self.owner_surface()
        if owner and owner.inject_session:
            try:
                owner.inject_session(text)
            except Exception:
                pass


class LumaKitService:
    """Single owner of reminders, tasks, heartbeat, email, and the router."""

    def __init__(
        self,
        *,
        reminder_interval: int = 30,
        task_interval: int = 60,
        heartbeat_interval: int = 900,
        heartbeat_cooldown: int = 3600,
        email_interval: int = 60,
    ):
        self.router = NotificationRouter()
        self._reminder_interval = reminder_interval
        self._task_interval = task_interval
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_cooldown = heartbeat_cooldown
        self._email_interval = email_interval
        self._reminders: ReminderChecker | None = None
        self._tasks: TaskRunner | None = None
        self._heartbeat: Heartbeat | None = None
        self._email: EmailChecker | None = None
        self._started = False

    def register_surface(self, surface: Surface) -> None:
        self.router.register(surface)

    def notify_activity(self) -> None:
        if self._heartbeat:
            self._heartbeat.notify_activity()

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        def on_reminder(reminder):
            # Reminders always fan out across registered user-facing surfaces:
            # web + Telegram when both are configured, regardless of where the
            # reminder was created.
            target = "both"
            label = "Family reminder" if reminder.get("chat_id") is None else "Reminder"
            web_user_id = (
                None
                if reminder.get("chat_id") is None
                else chat_owner_id(reminder.get("chat_id"))
            )
            # Log first so a missed ping is recoverable on whichever surface
            # the user opens next.
            notification_id = notifications.log(
                content=reminder["content"],
                label=label,
                user_id=web_user_id,
            )
            return self.router.route({
                "content": reminder["content"],
                "label": label,
                "chat_id": reminder.get("chat_id"),
                "web_user_id": web_user_id,
                "target": target,
                "notification_id": notification_id,
            })

        self._reminders = ReminderChecker(interval=self._reminder_interval, notify=on_reminder)
        self._reminders.start()

        def on_task(msg, chat_id=None):
            self.router.route({
                "content": msg,
                "label": "",
                "chat_id": chat_id,
                "target": "auto",
            })

        self._tasks = TaskRunner(interval=self._task_interval, notify=on_task)
        self._tasks.start()

        self._heartbeat = Heartbeat(
            send=lambda msg: self.router.notify_owner(msg),
            interval=self._heartbeat_interval,
            cooldown=self._heartbeat_cooldown,
            inject_session=self.router.inject_owner_session,
            owner_chat_id=auth.get_owner(),
        )
        self._heartbeat.start()

        def email_ask_llm(prompt):
            cfg = get_effective_config_for_user(auth.get_owner())
            client = OllamaClient(fallback_model=cfg["fallback_model"])
            response = client.chat(
                model=cfg["primary_model"],
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                deadline=90,
                priority="medium",
            )
            return response.get("message", {}).get("content", "").strip()

        def log_email_notification(msg, meta=None):
            return notifications.log(
                content=msg,
                label="",
                user_id=auth.get_owner(),
                meta=meta,
            )

        def on_email_notification(msg, meta=None, notification_id=None):
            return self.router.route({
                "content": msg,
                "label": "",
                "chat_id": auth.get_owner(),
                "target": "auto",
                "notification_id": notification_id,
                "meta": meta or {},
            })

        self._email = EmailChecker(
            notify_owner=on_email_notification,
            ask_llm=email_ask_llm,
            inject_session=self.router.inject_owner_session,
            log_notification=log_email_notification,
            interval=self._email_interval,
        )
        self._email.start()

    def stop(self) -> None:
        for worker in (self._reminders, self._tasks, self._heartbeat, self._email):
            if worker is None:
                continue
            try:
                worker.stop()
            except Exception:
                pass
        self._reminders = self._tasks = self._heartbeat = self._email = None
        self._started = False

    @property
    def email(self) -> EmailChecker | None:
        return self._email
