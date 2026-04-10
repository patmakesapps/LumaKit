"""EmailChecker — polls Lumi's inbox once per hour and pings the owner
on Telegram when new messages arrive.

Tracks the highest UID seen so we never re-notify on the same message.
Owner is the only person who ever gets these notifications.
"""

import email
import imaplib
import os
import threading
from email.utils import parseaddr


class EmailChecker:
    def __init__(self, send, interval=3600):
        """
        send     — callable(text) to message the owner
        interval — seconds between checks (default 1 hour)
        """
        self._send = send
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._last_uid = 0  # highest UID we've already notified on

    def _config(self):
        return {
            "address": os.getenv("LUMI_EMAIL_ADDRESS", "").strip(),
            "password": os.getenv("LUMI_EMAIL_PASSWORD", "").strip().replace(" ", ""),
            "imap_host": os.getenv("LUMI_EMAIL_IMAP_HOST", "imap.gmail.com").strip(),
            "imap_port": int(os.getenv("LUMI_EMAIL_IMAP_PORT", "993")),
        }

    def _fetch_new(self):
        """Return list of (uid, from, subject) for messages newer than last_uid."""
        cfg = self._config()
        if not cfg["address"] or not cfg["password"]:
            return []

        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"]) as imap:
            imap.login(cfg["address"], cfg["password"])
            imap.select("INBOX")

            # On first run, seed last_uid with the current highest and return nothing.
            # This avoids spamming the owner with every historical message on startup.
            if self._last_uid == 0:
                status, data = imap.uid("search", None, "ALL")
                if status == "OK" and data and data[0]:
                    uids = [int(u) for u in data[0].split()]
                    if uids:
                        self._last_uid = max(uids)
                return []

            # Search for UIDs strictly greater than last_uid
            status, data = imap.uid("search", None, f"UID {self._last_uid + 1}:*")
            if status != "OK" or not data or not data[0]:
                return []

            uids = [int(u) for u in data[0].split() if int(u) > self._last_uid]
            if not uids:
                return []

            new_messages = []
            for uid in sorted(uids):
                status, msg_data = imap.uid("fetch", str(uid).encode(), "(BODY.PEEK[HEADER])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                parsed = email.message_from_bytes(msg_data[0][1])
                from_name, from_addr = parseaddr(parsed.get("From", ""))
                sender = from_name or from_addr or "unknown"
                subject = parsed.get("Subject", "(no subject)")
                new_messages.append((uid, sender, subject))

            if uids:
                self._last_uid = max(self._last_uid, max(uids))

            return new_messages

    def _pulse(self):
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            try:
                new_messages = self._fetch_new()
                if not new_messages:
                    continue
                if len(new_messages) == 1:
                    uid, sender, subject = new_messages[0]
                    self._send(f"New email from {sender}: {subject}")
                else:
                    lines = [f"{len(new_messages)} new emails:"]
                    for _, sender, subject in new_messages[:5]:
                        lines.append(f"- {sender}: {subject}")
                    if len(new_messages) > 5:
                        lines.append(f"...and {len(new_messages) - 5} more")
                    self._send("\n".join(lines))
            except Exception as e:
                print(f"[email checker error] {e}")

    def start(self):
        # Prime last_uid on startup without waiting a full interval
        try:
            self._fetch_new()
        except Exception as e:
            print(f"[email checker init error] {e}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._pulse, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
