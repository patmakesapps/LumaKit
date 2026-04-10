"""EmailChecker — polls Lumi's inbox every 60 seconds, notifies the owner
on Telegram when new mail arrives, and asks the LLM to write a natural-language
summary (with a draft reply if it looks warranted).

No structured parsing. The LLM's response is passed through verbatim.
If the LLM fails or times out, we fall back to showing the raw (URL-stripped)
body so the owner still gets the actual email content.

All URLs are stripped from the body before the LLM ever sees it. The full URL
list is shown to the owner separately on Telegram.
"""

import email as email_module
import imaplib
import os
import threading
from email.header import decode_header, make_header
from email.utils import parseaddr

from core.email_filter import strip_urls


# --------- low-level helpers ---------

def _config():
    return {
        "address": os.getenv("LUMI_EMAIL_ADDRESS", "").strip(),
        "password": os.getenv("LUMI_EMAIL_PASSWORD", "").strip().replace(" ", ""),
        "imap_host": os.getenv("LUMI_EMAIL_IMAP_HOST", "imap.gmail.com").strip(),
        "imap_port": int(os.getenv("LUMI_EMAIL_IMAP_PORT", "993")),
    }


def _decode_header(value):
    """Decode MIME-encoded email headers (Subject, From, etc.) to plain string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg):
    """Pull text from an email message. Returns (text, is_html)."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    return (
                        part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        ),
                        False,
                    )
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return (
                        part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        ),
                        True,
                    )
                except Exception:
                    continue
        return "", False
    try:
        ctype = msg.get_content_type()
        return (
            msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            ),
            ctype == "text/html",
        )
    except Exception:
        return str(msg.get_payload()), False


# --------- checker class ---------

class EmailChecker:
    def __init__(self, notify_owner, ask_llm, inject_session=None, interval=60):
        """
        notify_owner    — callable(text) to send a Telegram message to the owner
        ask_llm         — callable(prompt) returning the LLM's text response
        inject_session  — optional callable(text) that appends an assistant message
                          to the owner's agent session
        interval        — seconds between polls (default 60)
        """
        self._notify = notify_owner
        self._ask_llm = ask_llm
        self._inject = inject_session
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._last_uid = 0
        # Pending drafts: list of {to, subject, body, from_label, uid} dicts —
        # appended when the LLM drafts a reply. Bridge intercepts owner's "yes/no"
        # to send or discard the most recently presented draft. Using a list so
        # multiple drafts from simultaneous emails don't overwrite each other.
        self.pending_drafts = []

    def _fetch_new_messages(self):
        """Return list of parsed message dicts newer than last_uid."""
        cfg = _config()
        if not cfg["address"] or not cfg["password"]:
            return []

        messages = []
        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"]) as imap:
            imap.login(cfg["address"], cfg["password"])
            imap.select("INBOX")

            # On first run, seed last_uid to current max and return nothing.
            if self._last_uid == 0:
                status, data = imap.uid("search", None, "ALL")
                if status == "OK" and data and data[0]:
                    uids = [int(u) for u in data[0].split()]
                    if uids:
                        self._last_uid = max(uids)
                return []

            status, data = imap.uid("search", None, f"UID {self._last_uid + 1}:*")
            if status != "OK" or not data or not data[0]:
                return []

            uids = sorted(int(u) for u in data[0].split() if int(u) > self._last_uid)
            if not uids:
                return []

            for uid in uids:
                status, msg_data = imap.uid("fetch", str(uid).encode(), "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                parsed = email_module.message_from_bytes(msg_data[0][1])
                from_name, from_addr = parseaddr(parsed.get("From", ""))
                from_name = _decode_header(from_name)
                subject = _decode_header(parsed.get("Subject", ""))
                raw_body, is_html = _extract_body(parsed)
                sanitized_body, links = strip_urls(raw_body, is_html=is_html)

                messages.append({
                    "uid": uid,
                    "from_name": from_name or "",
                    "from_addr": from_addr or "",
                    "subject": subject,
                    "date": parsed.get("Date", ""),
                    "body": sanitized_body[:3000],
                    "links": links,
                })

            if uids:
                self._last_uid = max(self._last_uid, max(uids))

        return messages

    def _mark_read(self, uid):
        """Flag a message as \\Seen on the server."""
        cfg = _config()
        try:
            with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"]) as imap:
                imap.login(cfg["address"], cfg["password"])
                imap.select("INBOX")
                imap.uid("store", str(uid).encode(), "+FLAGS", "(\\Seen)")
        except Exception as e:
            print(f"[email mark-read error] {e}")

    def _ask_llm_about_email(self, msg):
        """Ask the LLM to write a natural-language summary of the email.
        Returns the LLM's text verbatim, or None if it fails.
        """
        sender = f"{msg['from_name']} <{msg['from_addr']}>".strip() if msg['from_name'] else msg['from_addr']
        prompt = (
            "A new email just arrived for Pat. Write a short, casual Telegram message "
            "telling Pat about it like a friend would. Use slang and be natural.\n\n"
            "If the email clearly wants a reply (asks a question, makes a request, invites "
            "him somewhere), include a draft reply at the bottom under a 'draft:' line that "
            "Pat can send. Keep drafts short, casual, in Pat's voice.\n\n"
            "If it's just a notification, receipt, or newsletter, skip the draft and just "
            "tell Pat what it is.\n\n"
            "NEVER mention how you work, your tools, your code, or anything technical about "
            "yourself. NEVER ask about URLs — all URLs in the body below have been replaced "
            "with [link] for security. Just write for Pat.\n\n"
            f"From: {sender}\n"
            f"Subject: {msg['subject'] or '(no subject)'}\n"
            f"Body:\n{msg['body'] or '(empty body)'}\n"
        )

        try:
            response = self._ask_llm(prompt)
            return response.strip() if response else None
        except Exception as e:
            print(f"[email llm error] {e}")
            return None

    def _split_summary_and_draft(self, llm_text):
        """Split LLM text on 'draft:' (case-insensitive, line-start).
        Returns (summary_text, draft_text_or_none).
        """
        if not llm_text:
            return "", None
        lower = llm_text.lower()
        # Look for "draft:" at start of a line (or at very start of text)
        marker = None
        for candidate in ("\ndraft:", "\ndraft :", "\ndraft -"):
            idx = lower.find(candidate)
            if idx != -1:
                marker = (idx, len(candidate))
                break
        if marker is None and lower.startswith("draft:"):
            marker = (0, len("draft:"))
        if marker is None:
            return llm_text.strip(), None
        start, length = marker
        summary = llm_text[:start].strip()
        draft = llm_text[start + length:].strip()
        if not draft:
            return summary, None
        return summary, draft

    def _format_notification(self, msg, summary, draft):
        """Build the Telegram notification."""
        sender = f"{msg['from_name']} <{msg['from_addr']}>".strip() if msg['from_name'] else msg['from_addr']

        lines = [
            "📧 New email",
            f"From: {sender}",
            f"Subject: {msg['subject']}",
        ]

        if msg["body"]:
            lines.extend(["", "─── body ───", msg["body"], "─────────────"])

        if summary:
            lines.extend(["", summary])

        if draft:
            lines.extend([
                "",
                "─── draft reply ───",
                draft,
                "────────────────────",
                "Reply 'yes' to send it, or 'no' to skip.",
            ])

        if msg["links"]:
            lines.extend(["", "⚠️ Links Lumi could not see:"])
            for url, label in msg["links"][:10]:
                lines.append(f"• {url}  ({label})")
            if len(msg["links"]) > 10:
                lines.append(f"...and {len(msg['links']) - 10} more")

        return "\n".join(lines)

    def _handle_message(self, msg):
        llm_text = self._ask_llm_about_email(msg)
        summary, draft = self._split_summary_and_draft(llm_text)

        # If we got a draft, append it as pending so the owner can one-shot approve
        if draft and msg["from_addr"]:
            self.pending_drafts.append({
                "to": msg["from_addr"],
                "subject": msg["subject"] if msg["subject"].lower().startswith("re:") else f"Re: {msg['subject'] or '(no subject)'}",
                "body": draft,
                "from_label": msg["from_name"] or msg["from_addr"],
                "uid": msg["uid"],
            })

        notification = self._format_notification(msg, summary, draft)
        self._notify(notification)
        print(f"[email] notified owner about: {msg['subject'] or '(no subject)'}" + (" [draft pending]" if draft else ""))

        # Inject into owner's session so "reply to that" works if they don't just say yes/no
        if self._inject:
            try:
                sender = f"{msg['from_name']} <{msg['from_addr']}>".strip() if msg['from_name'] else msg['from_addr']
                injection = (
                    f"[Background email check] A new email arrived (uid {msg['uid']}).\n"
                    f"From: {sender}\n"
                    f"Subject: {msg['subject']}\n"
                    f"Body: {msg['body']}"
                )
                if summary:
                    injection += f"\n\nMy take: {summary}"
                if draft:
                    injection += f"\n\nI drafted this reply, pending approval:\n{draft}"
                self._inject(injection)
            except Exception as e:
                print(f"[session inject error] {e}")

        self._mark_read(msg["uid"])

    def clear_pending_draft(self):
        """Remove the most recently presented pending draft."""
        if self.pending_drafts:
            self.pending_drafts.pop()

    @property
    def pending_draft(self):
        """Backward-compatible property: returns the most recent pending draft, or None."""
        return self.pending_drafts[-1] if self.pending_drafts else None

    def _pulse(self):
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            try:
                new_messages = self._fetch_new_messages()
                for msg in new_messages:
                    self._handle_message(msg)
            except Exception as e:
                print(f"[email checker error] {e}")

    def start(self):
        try:
            self._fetch_new_messages()
            print(f"[email checker] seeded last_uid={self._last_uid}, polling every {self._interval}s")
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
