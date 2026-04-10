"""Email tools — send and read mail from Lumi's own account.

Owner-only: all tools check core.auth.is_owner_active() and refuse
unless the current Telegram user is the owner. Uses SMTP over SSL for
sending and IMAP over SSL for reading.

Config (.env):
    LUMI_EMAIL_ADDRESS       — Lumi's email address
    LUMI_EMAIL_PASSWORD      — app password (Gmail) or account password
    LUMI_EMAIL_SMTP_HOST     — default: smtp.gmail.com
    LUMI_EMAIL_SMTP_PORT     — default: 465
    LUMI_EMAIL_IMAP_HOST     — default: imap.gmail.com
    LUMI_EMAIL_IMAP_PORT     — default: 993
"""

import email
import imaplib
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from core import auth


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


# ---------- send ----------


def get_email_send_tool():
    return {
        "name": "email_send",
        "description": "Sends an email from Lumi's own email account. Owner-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address (comma-separated for multiple)"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text)"},
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
    msg.set_content(inputs["body"])

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context, timeout=30) as smtp:
            smtp.login(cfg["address"], cfg["password"])
            smtp.send_message(msg)
        return {"sent": True, "to": inputs["to"], "subject": inputs["subject"]}
    except Exception as e:
        return {"error": f"SMTP error: {e}"}


# ---------- inbox list ----------


def get_email_check_inbox_tool():
    return {
        "name": "email_check_inbox",
        "description": "Lists recent emails from Lumi's inbox. Owner-only.",
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
        "description": "Reads the full body of a specific email by id from Lumi's inbox. Owner-only.",
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
    """Pull plain text from an email message (falls back to html text if needed)."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return str(msg.get_payload())


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
            return {
                "id": msg_id,
                "from": f"{from_name} <{from_addr}>" if from_name else from_addr,
                "to": parsed.get("To", ""),
                "subject": parsed.get("Subject", ""),
                "date": parsed.get("Date", ""),
                "body": _extract_body(parsed)[:10000],  # cap at 10k chars
            }
    except Exception as e:
        return {"error": f"IMAP error: {e}"}


# ---------- reply ----------


def get_email_reply_tool():
    return {
        "name": "email_reply",
        "description": "Replies to an email by id. Owner-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Message id to reply to"},
                "body": {"type": "string", "description": "Reply body (plain text)"},
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

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context, timeout=30) as smtp:
            smtp.login(cfg["address"], cfg["password"])
            smtp.send_message(reply)
        return {"sent": True, "to": reply_to, "subject": subject}
    except Exception as e:
        return {"error": f"SMTP error: {e}"}
