"""Email safety layer — URL stripping, codebase-leak detection, rate limiting, audit log.

All outbound email goes through scan_for_leaks + check_rate_limit before sending.
All inbound bodies go through strip_urls before the LLM sees them.
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from html.parser import HTMLParser


AUDIT_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".lumakit",
    "sent_emails.log",
)


# ---------- URL stripping ----------

# Matches http(s)://, www., and bare domains like evil.com/path
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|io|gov|edu|co|uk|me|ru|xyz|info|biz|app|dev|ai|tv|us|ca|au|de|fr|jp|cn|in|br)(?:/\S*)?",
    re.IGNORECASE,
)


class _LinkStripParser(HTMLParser):
    """Walks HTML, replaces every <a>/<img>/<script>/<style> with safe placeholders.

    For anchors, BOTH the href AND the visible text are obliterated so the LLM
    can't be tricked by "Click here" style phishing. Visible text is captured
    alongside the href so the owner can review it on Telegram.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces = []
        self.links = []  # list of (href, visible_text)
        self._skip_depth = 0  # >0 means we're inside a tag whose text we skip
        self._anchor_stack = []  # stack of (href, text_pieces)

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "a":
            href = attrs_dict.get("href", "") or ""
            self._anchor_stack.append((href, []))
            self._skip_depth += 1
            self.pieces.append("[link]")
        elif tag == "img":
            src = attrs_dict.get("src", "") or ""
            alt = attrs_dict.get("alt", "") or ""
            if src:
                self.links.append((src, f"(image: {alt})" if alt else "(image)"))
            self.pieces.append("[image]")
        elif tag in ("script", "style", "head", "meta"):
            self._skip_depth += 1
        elif tag == "form":
            action = attrs_dict.get("action", "") or ""
            if action:
                self.links.append((action, "(form action)"))

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor_stack:
            href, text_pieces = self._anchor_stack.pop()
            visible = "".join(text_pieces).strip()
            if href or visible:
                self.links.append((href or "(no href)", visible or "(no text)"))
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag in ("script", "style", "head", "meta"):
            if self._skip_depth > 0:
                self._skip_depth -= 1

    def handle_data(self, data):
        if self._anchor_stack:
            # Capture anchor visible text for owner review, but don't emit to LLM
            self._anchor_stack[-1][1].append(data)
            return
        if self._skip_depth > 0:
            return
        self.pieces.append(data)


def strip_urls(body, is_html=False):
    """Remove every URL/link/script from a body so the LLM can't see or fetch it.

    Returns (sanitized_text, links) where links is a list of (url, label) tuples.
    The owner sees the full list; the LLM only sees the sanitized text.
    """
    if not body:
        return "", []

    links = []

    if is_html:
        parser = _LinkStripParser()
        try:
            parser.feed(body)
            parser.close()
        except Exception:
            # Malformed HTML — fall through to plain-text stripping
            pass
        text = "".join(parser.pieces)
        links.extend(parser.links)
    else:
        text = body

    # Second pass: strip any bare URLs that weren't inside HTML tags
    def _capture(match):
        links.append((match.group(0), "(bare url)"))
        return "[link]"

    text = _URL_RE.sub(_capture, text)

    # Collapse excessive whitespace created by stripping
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip(), links


# ---------- Codebase-leak scanner ----------

# Patterns that should NEVER appear in an outbound email
_LEAK_PATTERNS = [
    (re.compile(r"\b(?:OLLAMA|TELEGRAM|SERPAPI|LUMI_EMAIL)_[A-Z_]+\b"), "environment variable name"),
    (re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{10,}"), "api token pattern"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "long hex string (possible secret)"),
    (re.compile(r"[A-Za-z]:\\[A-Za-z0-9_\\. -]+"), "windows file path"),
    (re.compile(r"(?:^|\s)/(?:home|Users|etc|var|usr|opt)/[A-Za-z0-9_./-]+"), "unix file path"),
    (re.compile(r"\b(?:core|tools|memory)/[a-z_]+\.py\b"), "source file path"),
    (re.compile(r"\blumakit\b", re.IGNORECASE), "internal project name"),
    (re.compile(r"\bollama\b", re.IGNORECASE), "internal model runtime"),
    (re.compile(r"\bclaude\b|\banthropic\b", re.IGNORECASE), "underlying model brand"),
    (re.compile(r"\bsystem prompt\b", re.IGNORECASE), "prompt engineering term"),
    (re.compile(r"\bsource code\b|\bcodebase\b|\brepository\b", re.IGNORECASE), "codebase reference"),
    (re.compile(r"\btool_registry\b|\btool_call\b|\bexecute_tool\b"), "internal tool api"),
    (re.compile(r"\b(?:import|def|class|return)\s+\w+", re.IGNORECASE), "python keyword usage"),
]


def scan_for_leaks(subject, body):
    """Return a list of (pattern_label, matched_text) for anything that looks like a leak.
    Empty list = safe to send.
    """
    combined = f"{subject}\n{body}"
    hits = []
    for pattern, label in _LEAK_PATTERNS:
        match = pattern.search(combined)
        if match:
            hits.append((label, match.group(0)))
    return hits


# ---------- Rate limiter ----------

_rate_lock = threading.Lock()
_send_timestamps = []  # rolling list of unix timestamps of recent sends


def _max_per_hour():
    try:
        return int(os.getenv("LUMI_EMAIL_MAX_PER_HOUR", "10"))
    except ValueError:
        return 10


def check_rate_limit():
    """Return (allowed, remaining). allowed=False means too many sends in the last hour."""
    with _rate_lock:
        now = time.time()
        cutoff = now - 3600
        _send_timestamps[:] = [t for t in _send_timestamps if t > cutoff]
        limit = _max_per_hour()
        remaining = limit - len(_send_timestamps)
        return remaining > 0, remaining


def record_send():
    """Call this AFTER a successful send to count it against the rate limit."""
    with _rate_lock:
        _send_timestamps.append(time.time())


# ---------- Audit log ----------

def audit_log(to, subject, body, approved, error=None):
    """Append a JSON line for every send attempt (approved or not)."""
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "to": to,
        "subject": subject,
        "body": body,
        "approved": approved,
        "error": error,
    }
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[audit log error] {e}")


# ---------- Signature ----------

def apply_signature(body):
    """Append the configured signature to the body."""
    sig = os.getenv("LUMI_EMAIL_SIGNATURE", "Lumi - official LumaKit agent").strip()
    if not sig:
        return body
    if sig in body:
        return body
    return f"{body.rstrip()}\n\n— {sig}"
