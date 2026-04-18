"""Email tools — send and read mail from Lumi's own account.

Owner-only: all tools check core.auth.is_owner_active() and refuse
unless the current Telegram user is the owner. Uses SMTP over SSL for
sending and IMAP over SSL for reading.

Safety layers (all outbound):
  1. Owner gate
  2. Codebase-leak scanner (scan_for_leaks)
  3. Rate limit (10/hour default, check_rate_limit)
  4. Interactive confirmation (cli.confirm shows full draft on Telegram)
  5. Signature applied automatically
  6. Audit log entry (.lumakit/sent_emails.log)

Safety layers (all inbound):
  1. Owner gate
  2. URL stripping — HTML <a href>/<img>/<script>/<style> obliterated,
     plain-text URLs replaced with [link]. Links returned separately for
     the owner, never visible to the LLM.

Config (.env):
    LUMI_EMAIL_ADDRESS       — Lumi's email address
    LUMI_EMAIL_PASSWORD      — app password (Gmail) or account password
    LUMI_EMAIL_SMTP_HOST     — default: smtp.gmail.com
    LUMI_EMAIL_SMTP_PORT     — default: 465
    LUMI_EMAIL_IMAP_HOST     — default: imap.gmail.com
    LUMI_EMAIL_IMAP_PORT     — default: 993
    LUMI_EMAIL_SIGNATURE     — appended to every outbound body
    LUMI_EMAIL_MAX_PER_HOUR  — rate limit on outbound sends (default 10)
"""

import email
import imaplib
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

from core import auth, cli
from core.email_filter import (
    apply_signature,
    audit_log,
    check_rate_limit,
    record_send,
    scan_for_leaks,
    strip_urls,
)


def _owner_only_error():
    return {"error": "This tool is owner-only. Current user is not the owner."}


def _config():
    return {
        "address": os.getenv("LUMI_EMAIL_ADDRESS", "").strip(),
        "password": os.getenv("LUMI_EMAIL_PASSWORD", "").strip().replace(" ", ""),
        "smtp_host": os.getenv("LUMI_EMAIL_SMTP_HOST", "smtp.gmail.com").strip(),
        "smtp_port": int(os.getenv("LUMI_EMAIL_SMTP_PORT", "465")),
        "imap_host": os.getenv("LUMI_EMAIL_IMAP_HOST", "imap.gmail.com").strip(),
        "imap_port": int(os.getenv("LUMI_EMAIL_IMAP_PORT", "993")),
    }


def _require_config(cfg):
    if not cfg["address"] or not cfg["password"]:
        return {"error": "LUMI_EMAIL_ADDRESS and LUMI_EMAIL_PASSWORD must be set in .env"}
    return None


# ---------- send pipeline (shared by send and reply) ----------


def send_preapproved(to, subject, body):
    """Send an email that has already been approved by the owner through another UI
    (e.g. the EmailChecker pending-draft notification). Still runs leak scan,
    rate limit, signature, and audit log — skips only the interactive confirm.

    Returns a result dict the same shape as the tool executes.
    """
    cfg = _config()
    err = _require_config(cfg)
    if err:
        return err

    # 1. Leak scan
    leaks = scan_for_leaks(subject, body)
    if leaks:
        reasons = "; ".join(f"{label}: {match!r}" for label, match in leaks)
        audit_log(to, subject, body, approved=False, error=f"leak scan tripped: {reasons}")
        return {"error": f"Blocked by codebase-leak filter. Tripped: {reasons}"}

    # 2. Rate limit
    allowed, remaining = check_rate_limit()
    if not allowed:
        audit_log(to, subject, body, approved=False, error="rate limit exceeded")
        return {"error": f"Rate limit: hit max emails per hour ({os.getenv('LUMI_EMAIL_MAX_PER_HOUR', '10')})."}

    # 3. Signature
    final_body = apply_signature(body)

    # 4. Build + send
    msg = EmailMessage()
    msg["From"] = cfg["address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(final_body)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context, timeout=30) as smtp:
            smtp.login(cfg["address"], cfg["password"])
            smtp.send_message(msg)
    except Exception as e:
        audit_log(to, subject, final_body, approved=True, error=f"SMTP: {e}")
        return {"error": f"SMTP error: {e}"}

    record_send()
    audit_log(to, subject, final_body, approved=True)
    return {"sent": True, "to": to, "subject": subject, "remaining": remaining - 1}


def _safe_send(cfg, msg_obj, to_display, subject, body):
    """Run the full safety pipeline then send via SMTP.

    Returns a tool-result dict. Handles scan, rate limit, confirm, signature,
    audit, and actual SMTP delivery.
    """
    # 1. Codebase leak scan
    leaks = scan_for_leaks(subject, body)
    if leaks:
        reasons = "; ".join(f"{label}: {match!r}" for label, match in leaks)
        audit_log(to_display, subject, body, approved=False, error=f"leak scan tripped: {reasons}")
        return {
            "error": (
                "Blocked by codebase-leak filter. The draft contained terms that "
                "must never appear in outbound email. Rewrite it with natural, "
                f"non-technical content. Tripped: {reasons}"
            )
        }

    # 2. Rate limit
    allowed, remaining = check_rate_limit()
    if not allowed:
        audit_log(to_display, subject, body, approved=False, error="rate limit exceeded")
        return {"error": f"Rate limit: hit max emails per hour ({os.getenv('LUMI_EMAIL_MAX_PER_HOUR', '10')}). Try later."}

    # 3. Apply signature BEFORE showing the draft, so owner sees what will actually send
    final_body = apply_signature(body)

    # 4. Owner confirmation — shows full draft, owner says y/n on Telegram
    prompt = (
        "Ready to send this email?\n"
        f"To: {to_display}\n"
        f"Subject: {subject}\n"
        "─────────\n"
        f"{final_body}\n"
        "─────────\n"
        f"(Sends remaining this hour: {remaining - 1})"
    )
    if not cli.confirm(prompt):
        audit_log(to_display, subject, final_body, approved=False, error="owner declined")
        return {"sent": False, "declined": True, "message": "Owner declined to send."}

    # 5. Update the message body to the signed version
    msg_obj.set_content(final_body)

    # 6. Send
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context, timeout=30) as smtp:
            smtp.login(cfg["address"], cfg["password"])
            smtp.send_message(msg_obj)
    except Exception as e:
        audit_log(to_display, subject, final_body, approved=True, error=f"SMTP: {e}")
        return {"error": f"SMTP error: {e}"}

    record_send()
    audit_log(to_display, subject, final_body, approved=True)
    return {"sent": True, "to": to_display, "subject": subject}


# ---------- send ----------


def get_email_send_tool():
    return {
        "name": "email_send",
        "description": (
            "Sends an email from Lumi's email account. "
            "Requires approval before sending. "
            "Never include source code, file paths, or internal details in the body."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address (comma-separated for multiple)"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text, natural human content only)"},
                "cc": {"type": "string", "description": "Optional CC addresses (comma-separated)"},
            },
            "required": ["to", "subject", "body"],
        },
        "execute": _email_send,
    }


def _email_send(inputs):
    if not auth.is_owner_active():
        return _owner_only_error()

    cfg = _config()
    err = _require_config(cfg)
    if err:
        return err

    msg = EmailMessage()
    msg["From"] = cfg["address"]
    msg["To"] = inputs["to"]
    msg["Subject"] = inputs["subject"]
    if inputs.get("cc"):
        msg["Cc"] = inputs["cc"]
    # Body gets set inside _safe_send after signature applied
    msg.set_content(inputs["body"])

    return _safe_send(cfg, msg, inputs["to"], inputs["subject"], inputs["body"])


# ---------- inbox list ----------


def get_email_check_inbox_tool():
    return {
        "name": "email_check_inbox",
        "description": "Lists recent emails (metadata only — no body or URLs).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max number of messages to return (default 10)"},
                "unread_only": {"type": "boolean", "description": "Only return unread messages (default false)"},
            },
            "required": [],
        },
        "execute": _email_check_inbox,
    }


def _email_check_inbox(inputs):
    if not auth.is_owner_active():
        return _owner_only_error()

    cfg = _config()
    err = _require_config(cfg)
    if err:
        return err

    limit = int(inputs.get("limit", 10))
    unread_only = bool(inputs.get("unread_only", False))

    try:
        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"]) as imap:
            imap.login(cfg["address"], cfg["password"])
            imap.select("INBOX")

            criteria = "UNSEEN" if unread_only else "ALL"
            status, data = imap.search(None, criteria)
            if status != "OK":
                return {"error": f"IMAP search failed: {status}"}

            ids = data[0].split()
            ids = ids[-limit:][::-1]  # newest first

            messages = []
            for msg_id in ids:
                status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                header_bytes = msg_data[0][1]
                parsed = email.message_from_bytes(header_bytes)
                from_name, from_addr = parseaddr(parsed.get("From", ""))
                messages.append({
                    "id": msg_id.decode(),
                    "from": f"{from_name} <{from_addr}>" if from_name else from_addr,
                    "subject": parsed.get("Subject", ""),
                    "date": parsed.get("Date", ""),
                })

            return {"count": len(messages), "messages": messages}
    except Exception as e:
        return {"error": f"IMAP error: {e}"}


# ---------- read one ----------


def get_email_read_tool():
    return {
        "name": "email_read",
        "description": (
            "Reads the body of an email by id. All URLs are stripped before return — "
            "you will only see [link] placeholders. Do not ask for the URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Message id (from email_check_inbox)"},
            },
            "required": ["id"],
        },
        "execute": _email_read,
    }


def _extract_body(msg):
    """Pull text from an email. Returns (text, is_html)."""
    if msg.is_multipart():
        # Prefer plain text
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
        # Fall back to HTML
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


def _email_read(inputs):
    if not auth.is_owner_active():
        return _owner_only_error()

    cfg = _config()
    err = _require_config(cfg)
    if err:
        return err

    msg_id = inputs["id"]
    try:
        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"]) as imap:
            imap.login(cfg["address"], cfg["password"])
            imap.select("INBOX")
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return {"error": f"Could not fetch message {msg_id}"}
            parsed = email.message_from_bytes(msg_data[0][1])
            from_name, from_addr = parseaddr(parsed.get("From", ""))
            raw_body, is_html = _extract_body(parsed)
            sanitized_body, links = strip_urls(raw_body, is_html=is_html)
            return {
                "id": msg_id,
                "from": f"{from_name} <{from_addr}>" if from_name else from_addr,
                "to": parsed.get("To", ""),
                "subject": parsed.get("Subject", ""),
                "date": parsed.get("Date", ""),
                "body": sanitized_body[:10000],  # cap at 10k chars
                "link_count": len(links),
                "note": (
                    "All URLs have been stripped for safety. "
                    "If you need to discuss a link, tell the owner the [link] was there — "
                    "the owner sees the full URLs separately."
                ) if links else "",
            }
    except Exception as e:
        return {"error": f"IMAP error: {e}"}


# ---------- reply ----------


def get_email_reply_tool():
    return {
        "name": "email_reply",
        "description": (
            "Replies to an email by id. "
            "Requires approval before sending. "
            "Never include source code, file paths, or internal details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Message id to reply to"},
                "body": {"type": "string", "description": "Reply body (plain text, natural human content only)"},
            },
            "required": ["id", "body"],
        },
        "execute": _email_reply,
    }


def _email_reply(inputs):
    if not auth.is_owner_active():
        return _owner_only_error()

    cfg = _config()
    err = _require_config(cfg)
    if err:
        return err

    msg_id = inputs["id"]

    # Fetch headers of the original so we can set In-Reply-To / References / subject
    try:
        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"]) as imap:
            imap.login(cfg["address"], cfg["password"])
            imap.select("INBOX")
            status, data = imap.fetch(msg_id, "(BODY.PEEK[HEADER])")
            if status != "OK" or not data or not data[0]:
                return {"error": f"Could not fetch message {msg_id}"}
            parsed = email.message_from_bytes(data[0][1])
    except Exception as e:
        return {"error": f"IMAP fetch error: {e}"}

    _, reply_to = parseaddr(parsed.get("Reply-To") or parsed.get("From", ""))
    if not reply_to:
        return {"error": "Original message has no From/Reply-To address"}

    orig_subject = parsed.get("Subject", "")
    subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"

    reply = EmailMessage()
    reply["From"] = cfg["address"]
    reply["To"] = reply_to
    reply["Subject"] = subject
    if parsed.get("Message-ID"):
        reply["In-Reply-To"] = parsed["Message-ID"]
        existing_refs = parsed.get("References", "")
        reply["References"] = f"{existing_refs} {parsed['Message-ID']}".strip()
    reply.set_content(inputs["body"])

    return _safe_send(cfg, reply, reply_to, subject, inputs["body"])
