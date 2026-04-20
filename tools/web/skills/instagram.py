"""Instagram skill handlers.

Each handler runs its own tight observe -> act -> verify loop against a live
Playwright page. The point is that the *handler* sees the DOM between steps,
not the LLM — so it can recover from hashed classes, modals, and the other
SPA quirks that break stateless tool calls.

Every handler returns the same shape:
    {
        'status': 'done' | 'blocked' | 'needs_human',
        'narrative': one-line human summary,
        'blocked_reason': optional vocabulary word,
        'observations': dict of useful facts (final_url, matched_user, etc.),
        'no_progress': True if nothing mutated,
    }
"""

import re
import time

from core.interrupts import raise_if_interrupted


_INBOX_URL = 'https://www.instagram.com/direct/inbox/'
_LOGIN_HINT_TOKENS = ('login', 'accounts/login', 'sign-in', 'signin')


def _dismiss_blocking_modals(page) -> list[str]:
    """Close the "Turn on Notifications" / "Save Login Info" modals if present."""
    dismissed = []
    for label in ('Not Now', 'Not now'):
        try:
            btn = page.locator(f"button:has-text('{label}')").first
            if btn.is_visible(timeout=500):
                btn.click(timeout=1500)
                dismissed.append(label)
                page.wait_for_timeout(400)
        except Exception:
            continue
    return dismissed


def _looks_like_login_wall(page) -> bool:
    try:
        url = (page.url or '').lower()
    except Exception:
        return False
    return any(tok in url for tok in _LOGIN_HINT_TOKENS)


def _blocked(reason: str, narrative: str, **extra) -> dict:
    out = {
        'status': 'blocked',
        'blocked_reason': reason,
        'narrative': narrative,
        'no_progress': True,
    }
    if extra:
        out['observations'] = extra
    return out


def _done(narrative: str, **observations) -> dict:
    return {
        'status': 'done',
        'narrative': narrative,
        'no_progress': False,
        'observations': observations,
    }


def _navigate_to_inbox(page) -> None:
    if '/direct/' not in (page.url or ''):
        page.goto(_INBOX_URL, wait_until='domcontentloaded')
        page.wait_for_timeout(1200)
    _dismiss_blocking_modals(page)


def _collect_thread_rows(page) -> list[dict]:
    """Return the DM thread list as {name, preview, selector_hint, x, y} rows."""
    js = r"""
    () => {
        const rows = [];
        const listItems = document.querySelectorAll('div[role="listitem"], a[href^="/direct/t/"]');
        const seen = new Set();
        listItems.forEach((el) => {
            const rect = el.getBoundingClientRect();
            if (rect.width < 50 || rect.height < 30) return;
            const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (!text) return;
            const firstLine = text.split('\n')[0].slice(0, 80);
            const href = el.getAttribute('href') || (el.closest('a') && el.closest('a').getAttribute('href')) || null;
            const key = `${firstLine}|${Math.round(rect.top)}`;
            if (seen.has(key)) return;
            seen.add(key);
            rows.push({
                name: firstLine,
                preview: text.slice(0, 200),
                href: href,
                x: Math.round(rect.left + rect.width / 2),
                y: Math.round(rect.top + rect.height / 2),
            });
        });
        return rows;
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception:
        return []


def _match_thread(rows: list[dict], target: str) -> dict | None:
    needle = (target or '').strip().lower()
    if not needle:
        return None
    for row in rows:
        name = (row.get('name') or '').lower()
        if name == needle or name.startswith(needle) or needle in name:
            return row
    return None


def open_dm(page, *, target: str, **_) -> dict:
    """Navigate to the DM inbox and open the thread matching `target`."""
    raise_if_interrupted("browse interrupted by /stop.")
    if not target:
        return _blocked('target_not_found', 'open_dm needs a target username.')

    _navigate_to_inbox(page)
    if _looks_like_login_wall(page):
        return _blocked('auth_required',
                        'Instagram redirected to login. Run instagram_session first.',
                        final_url=page.url)

    rows = _collect_thread_rows(page)
    if not rows:
        return _blocked('target_not_found',
                        'DM inbox did not render any thread rows.',
                        final_url=page.url)

    match = _match_thread(rows, target)
    if not match:
        candidates = [r.get('name') for r in rows[:10] if r.get('name')]
        return _blocked('target_not_found',
                        f"No DM thread matching '{target}'.",
                        candidates=candidates,
                        final_url=page.url)

    if match.get('href'):
        page.goto(f"https://www.instagram.com{match['href']}", wait_until='domcontentloaded')
    else:
        page.mouse.click(match['x'], match['y'])
    page.wait_for_timeout(900)
    raise_if_interrupted("browse interrupted by /stop.")

    if '/direct/t/' not in (page.url or ''):
        return _blocked('target_not_found',
                        f"Clicked thread for '{target}' but URL did not move to a thread view.",
                        final_url=page.url)

    return _done(f"Opened DM thread with {match['name']}.",
                 matched_user=match['name'], final_url=page.url)


def _find_composer(page):
    """Return the contenteditable composer locator, or None."""
    selectors = [
        "div[role='textbox'][contenteditable='true']",
        "div[contenteditable='true'][aria-label*='Message']",
        "div[contenteditable='true']",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=800):
                return loc
        except Exception:
            continue
    return None


def send_dm(page, *, target: str, value: str, **_) -> dict:
    """Open a DM thread with `target` and send `value`. Verifies the send."""
    text = (value or '').strip()
    if not text:
        return _blocked('target_not_found', 'send_dm needs a value (the message text).')

    open_result = open_dm(page, target=target)
    if open_result.get('status') != 'done':
        return open_result

    raise_if_interrupted("browse interrupted by /stop.")
    composer = _find_composer(page)
    if composer is None:
        return _blocked('target_not_found',
                        'Opened the thread but could not find the message composer.',
                        final_url=page.url)

    try:
        composer.click(timeout=3000)
        page.keyboard.type(text, delay=30)
        page.wait_for_timeout(300)
    except Exception as exc:
        return _blocked('unknown', f'Failed to type into composer: {exc}',
                        final_url=page.url)

    send_button = page.locator("div[role='button']:has-text('Send')").first
    used_enter = False
    try:
        if send_button.is_visible(timeout=800):
            send_button.click(timeout=2000)
        else:
            page.keyboard.press('Enter')
            used_enter = True
    except Exception:
        page.keyboard.press('Enter')
        used_enter = True

    page.wait_for_timeout(900)
    raise_if_interrupted("browse interrupted by /stop.")

    try:
        body_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ''
    except Exception:
        body_text = ''
    delivered = text in body_text
    if not delivered:
        return _blocked('unknown',
                        f"Typed '{text}' but could not confirm it appeared in the thread.",
                        final_url=page.url, used_enter_to_send=used_enter)

    return _done(f"Sent '{text}' to {open_result['observations'].get('matched_user', target)}.",
                 sent_text=text,
                 matched_user=open_result['observations'].get('matched_user', target),
                 used_enter_to_send=used_enter,
                 final_url=page.url)


def check_inbox(page, *, limit: int = 10, **_) -> dict:
    """Return the top `limit` DM threads with name + preview."""
    _navigate_to_inbox(page)
    if _looks_like_login_wall(page):
        return _blocked('auth_required',
                        'Instagram redirected to login. Run instagram_session first.',
                        final_url=page.url)

    rows = _collect_thread_rows(page)
    if not rows:
        return _blocked('target_not_found',
                        'DM inbox did not render any thread rows.',
                        final_url=page.url)

    limit = max(1, min(int(limit or 10), 25))
    trimmed = rows[:limit]
    narrative = f"Read {len(trimmed)} DM threads from the inbox."
    return _done(narrative, threads=trimmed, final_url=page.url)
