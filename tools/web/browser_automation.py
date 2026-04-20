"""
Browser automation tool using Playwright.
Supports navigating to URLs, filling form fields, clicking buttons,
reading page text/links, uploading files, and submitting forms.
"""

import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from core.display import status as report_status
from core.interrupts import OperationInterrupted, raise_if_interrupted
from core.paths import get_data_dir, get_repo_root
from tools.web.site_adapters import landmark_selectors


# Don't launch Chromium if the machine is already under obvious memory pressure.
_MIN_AVAILABLE_RAM = 1_000_000_000  # ~1 GiB
_SESSION_IDLE_TIMEOUT_SECONDS = 20 * 60
_BROWSER_PROFILES_DIR = get_data_dir() / 'browser_profiles'

_PLAYWRIGHT = None
_BROWSER_SESSIONS = {}
_SESSIONS_LOCK = threading.RLock()

_SITE_SETTLE_DELAYS_MS = {
    'instagram': {
        'navigate': 1200,
        'click': 900,
        'click_at': 900,
        'type': 250,
        'set_input_files': 1200,
        'default': 400,
    },
}


def _browser_profile_path(profile: str) -> Path:
    """Resolve a persistent storage-state file path for the given profile name."""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', profile).strip('_.') or 'default'
    return _BROWSER_PROFILES_DIR / f'{safe}.json'


def _save_storage_state(context, path: Path) -> bool:
    """Persist cookies + localStorage so the next launch starts logged in."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(path))
        return True
    except Exception:
        return False


def _screenshots_dir() -> Path:
    """Return the screenshots folder, creating it if needed."""
    path = get_data_dir() / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _available_ram() -> int | None:
    """Return MemAvailable in bytes on Linux, or None if it can't be read."""
    meminfo = "/proc/meminfo"
    if not os.path.exists(meminfo):
        return None
    try:
        with open(meminfo, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _interruptible_wait(page, timeout_ms: int):
    """Sleep in short slices so /stop can interrupt explicit waits quickly."""
    remaining = max(0, int(timeout_ms))
    while remaining > 0:
        raise_if_interrupted("Browser automation interrupted by /stop.")
        step = min(remaining, 100)
        page.wait_for_timeout(step)
        remaining -= step


def _interruptible_wait_for_selector(page, selector: str, timeout_ms: int):
    """Poll for a selector with short timeouts so /stop can interrupt quickly."""
    deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000)
    while True:
        raise_if_interrupted("Browser automation interrupted by /stop.")
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError(f"Timeout {timeout_ms}ms exceeded while waiting for {selector}")
        page.wait_for_selector(selector, timeout=min(remaining_ms, 250), state='visible')
        return


def _ensure_playwright_started():
    """Start Playwright lazily so sessions can survive across tool calls."""
    global _PLAYWRIGHT
    with _SESSIONS_LOCK:
        if _PLAYWRIGHT is None:
            _PLAYWRIGHT = sync_playwright().start()
        return _PLAYWRIGHT


def _stop_playwright_if_idle_locked():
    """Shut down the Playwright driver if no sessions are alive.

    The driver node otherwise lives for the life of the Python process,
    holding ~110 MB RSS even when Lumi isn't browsing. Caller must hold
    _SESSIONS_LOCK. _ensure_playwright_started() will re-launch it lazily
    on the next call.
    """
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None or _BROWSER_SESSIONS:
        return
    try:
        _PLAYWRIGHT.stop()
    except Exception:
        pass
    _PLAYWRIGHT = None


def _launch_browser_components(overall_timeout, storage_state_path: Path | None = None):
    """Create a browser/context/page triple with the tool's standard settings."""
    available = _available_ram()
    if available is not None and available < _MIN_AVAILABLE_RAM:
        available_gb = round(available / (1024 ** 3), 2)
        required_gb = round(_MIN_AVAILABLE_RAM / (1024 ** 3), 2)
        raise RuntimeError(
            f'Not enough free RAM to safely launch Chromium '
            f'({available_gb} GiB available, need at least {required_gb} GiB).'
        )

    playwright = _ensure_playwright_started()
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            '--disable-dev-shm-usage',
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-background-timer-throttling',
        ],
    )
    context_kwargs = {
        'viewport': {'width': 1280, 'height': 720},
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
    }
    if storage_state_path is not None and storage_state_path.exists():
        context_kwargs['storage_state'] = str(storage_state_path)
    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    page.set_default_timeout(overall_timeout)
    return browser, context, page


def _close_browser_session_locked(session_id):
    """Close and remove a persistent browser session. Caller must hold the lock."""
    session = _BROWSER_SESSIONS.pop(session_id, None)
    if not session:
        return False

    context = session.get('context')
    browser = session.get('browser')
    storage_state_path = session.get('storage_state_path')

    if context is not None and storage_state_path is not None:
        _save_storage_state(context, storage_state_path)

    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    return True


def _cleanup_stale_sessions():
    """Close sessions that have been idle too long so they do not pile up."""
    now = time.time()
    with _SESSIONS_LOCK:
        stale_ids = [
            session_id
            for session_id, session in _BROWSER_SESSIONS.items()
            if now - session.get('last_used', now) > _SESSION_IDLE_TIMEOUT_SECONDS
        ]
        for session_id in stale_ids:
            _close_browser_session_locked(session_id)


def _get_or_create_browser_session(session_id, overall_timeout, storage_state_path: Path | None = None):
    """Return a persistent session keyed by session_id, creating it if needed."""
    _cleanup_stale_sessions()
    now = time.time()

    with _SESSIONS_LOCK:
        session = _BROWSER_SESSIONS.get(session_id)
        if session is not None:
            try:
                browser = session['browser']
                context = session['context']
                page = session['page']
                if not browser.is_connected():
                    raise RuntimeError('browser disconnected')
                if page.is_closed():
                    page = context.new_page()
                    session['page'] = page
                page.set_default_timeout(overall_timeout)
                session['last_used'] = now
                return session, False
            except Exception:
                _close_browser_session_locked(session_id)

        browser, context, page = _launch_browser_components(
            overall_timeout, storage_state_path=storage_state_path
        )
        session = {
            'browser': browser,
            'context': context,
            'page': page,
            'created_at': now,
            'last_used': now,
            'storage_state_path': storage_state_path,
        }
        _BROWSER_SESSIONS[session_id] = session
        return session, True


def _resolve_upload_path(raw_path: str) -> Path:
    """Resolve an upload path, supporting absolute and repo-relative paths."""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = get_repo_root() / candidate
    candidate = candidate.resolve()

    if not candidate.exists():
        raise FileNotFoundError(f'Upload file not found: {candidate}')
    if not candidate.is_file():
        raise ValueError(f'Upload path is not a file: {candidate}')
    return candidate


def _short_selector(selector: str | None) -> str:
    if not selector:
        return "the page"
    selector = str(selector).strip()
    return selector if len(selector) <= 80 else selector[:77] + "..."


def _describe_action(action: dict) -> str:
    action_type = action.get('type', 'action')
    selector = _short_selector(action.get('selector'))
    if action_type == 'fill':
        return f"Filling {selector}."
    if action_type == 'type':
        return f"Typing into {selector}."
    if action_type == 'click':
        return f"Clicking {selector}."
    if action_type == 'click_at':
        x = action.get('x')
        y = action.get('y')
        if x is not None and y is not None:
            return f"Clicking at coordinates ({round(float(x), 1)}, {round(float(y), 1)})."
        return "Clicking at a specific point on the page."
    if action_type == 'select':
        return f"Selecting an option in {selector}."
    if action_type == 'set_input_files':
        return f"Uploading a file through {selector}."
    if action_type == 'wait':
        timeout = int(action.get('timeout', 5000))
        return f"Waiting {round(timeout / 1000, 1)} seconds for the page to settle."
    if action_type == 'wait_for_selector':
        return f"Waiting for {selector} to appear."
    if action_type == 'screenshot':
        return "Capturing a screenshot."
    if action_type == 'scroll':
        return "Scrolling the page."
    if action_type == 'get_text':
        return f"Reading text from {selector}."
    if action_type == 'get_links':
        return f"Collecting links from {selector}."
    if action_type == 'inspect_forms':
        return f"Inspecting visible form fields on {selector}."
    if action_type == 'inspect_interactives':
        return f"Inspecting clickable elements on {selector}."
    return f"Running browser action: {action_type}."


def _current_site_name(*urls: str | None) -> str:
    for raw_url in urls:
        if not raw_url:
            continue
        try:
            host = urlparse(raw_url).netloc.lower()
        except Exception:
            host = ""
        if 'instagram.com' in host:
            return 'instagram'
    return 'generic'


def _site_settle_delay_ms(site_name: str, stage: str) -> int:
    profile = _SITE_SETTLE_DELAYS_MS.get(site_name)
    if not profile:
        return 0
    return int(profile.get(stage, profile.get('default', 0)))


def _maybe_settle_page(page, site_name: str, stage: str) -> None:
    delay_ms = _site_settle_delay_ms(site_name, stage)
    if delay_ms > 0:
        _interruptible_wait(page, delay_ms)


def _wait_for_optional_navigation(page) -> None:
    try:
        page.wait_for_load_state('domcontentloaded', timeout=5000)
        return
    except Exception:
        pass
    try:
        page.wait_for_load_state('networkidle', timeout=2000)
    except Exception:
        pass


def _click_locator_by_coordinates(page, locator) -> dict:
    box = locator.bounding_box()
    if not box:
        raise RuntimeError('Element has no visible bounding box for coordinate click.')
    x = box['x'] + (box['width'] / 2)
    y = box['y'] + (box['height'] / 2)
    page.mouse.click(x, y)
    return {'x': round(x, 1), 'y': round(y, 1)}


def _click_with_retries(page, selector: str, *, wait_for_navigation: bool = False) -> dict:
    locator = page.locator(selector).first
    try:
        locator.wait_for(state='attached', timeout=2500)
    except Exception:
        pass
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    attempts: list[str] = []

    def _record_failure(label: str, exc: Exception) -> None:
        attempts.append(f"{label}: {exc}")

    try:
        locator.click(timeout=5000)
        if wait_for_navigation:
            _wait_for_optional_navigation(page)
        return {'method': 'locator.click'}
    except Exception as exc:
        _record_failure('normal click', exc)

    report_status(
        f"That click on {_short_selector(selector)} did not work normally. Trying a stronger click."
    )
    try:
        locator.click(force=True, timeout=5000)
        if wait_for_navigation:
            _wait_for_optional_navigation(page)
        return {'method': 'locator.click(force=True)'}
    except Exception as exc:
        _record_failure('force click', exc)

    report_status(
        f"Force click also failed on {_short_selector(selector)}. Trying a real mouse click."
    )
    try:
        coords = _click_locator_by_coordinates(page, locator)
        if wait_for_navigation:
            _wait_for_optional_navigation(page)
        return {'method': 'mouse.click', **coords}
    except Exception as exc:
        _record_failure('coordinate click', exc)

    report_status(
        f"Mouse click did not work on {_short_selector(selector)}. Dispatching a click event."
    )
    try:
        locator.dispatch_event('click')
        if wait_for_navigation:
            _wait_for_optional_navigation(page)
        return {'method': 'dispatch_event(click)'}
    except Exception as exc:
        _record_failure('dispatch click', exc)

    report_status(
        f"Event dispatch also failed on {_short_selector(selector)}. Trying element.click()."
    )
    try:
        locator.evaluate("(el) => el.click()")
        if wait_for_navigation:
            _wait_for_optional_navigation(page)
        return {'method': 'element.click()'}
    except Exception as exc:
        _record_failure('element.click()', exc)

    raise RuntimeError("; ".join(attempts))


_LOGIN_HOST_HINTS = ('login', 'signin', 'sign-in', 'accounts.')
_NEEDS_HUMAN_MARKERS = (
    'captcha',
    'recaptcha',
    'hcaptcha',
    'are you human',
    'verify you are human',
    'two-factor',
    '2fa',
    'enter the code sent',
    'confirm your identity',
    'suspicious activity',
    'unusual login',
)


def _page_suggests_human(page) -> bool:
    try:
        text = (page.evaluate("() => document.body ? document.body.innerText : ''") or '').lower()
    except Exception:
        return False
    if not text:
        return False
    return any(marker in text for marker in _NEEDS_HUMAN_MARKERS)


def _classify_failure(action: dict, error: str, page) -> tuple[str, str]:
    """Classify a failed browser action into (blocked_reason, human_hint).

    The point is to give the model (and the UI) a stable vocabulary to react to,
    instead of eyeballing a raw exception string on every retry.
    """
    action_type = str(action.get('type') or '')
    err = (error or '').lower()
    selector = action.get('selector')

    if _page_suggests_human(page):
        return 'needs_human', (
            'The page is asking for a captcha, 2FA code, or similar human step. '
            'Ask the user to handle it — do not keep retrying.'
        )

    if 'timeout' in err and ('waiting for selector' in err or selector):
        return 'target_not_found', (
            'Target did not appear. Re-inspect the page with inspect_interactives or '
            'inspect_forms — do not retry the same selector blind.'
        )
    if 'timeout' in err:
        return 'timeout', 'The action timed out. Re-read the page before retrying.'
    if 'no node found' in err or 'resolved to 0 elements' in err or 'did not match' in err:
        return 'target_not_found', (
            'Selector matched nothing on the current page. Run inspect_interactives '
            'and use one of the returned suggested_selector values.'
        )
    if action_type == 'click_at' and ('bounding box' in err or 'out of' in err):
        return 'off_screen', 'Coordinates were off-screen. Scroll or re-inspect first.'
    try:
        current_url = (page.url or '').lower()
    except Exception:
        current_url = ''
    if any(token in current_url for token in _LOGIN_HOST_HINTS):
        return 'auth_required', (
            'The page redirected to a login flow. Ask the user before continuing '
            'or use the saved auth_profile.'
        )
    return 'unknown', 'Re-observe the page before retrying.'


def _build_recovery_hint(action_type: str, site_name: str) -> str:
    if action_type in {'fill', 'type', 'select'}:
        return (
            'Run inspect_forms again and use one of the returned suggested_selector values '
            'instead of guessing.'
        )
    if action_type in {'click', 'click_at'}:
        if site_name == 'instagram':
            return (
                'Run inspect_interactives to discover visible click targets. On Instagram DM '
                'rows and modal controls, use the returned coordinates with click_at if normal '
                'selector clicks keep failing.'
            )
        return (
            'Run inspect_interactives to discover visible click targets. If the correct target '
            'only exposes coordinates cleanly, retry with click_at.'
        )
    return 'Inspect the page again before retrying so the next step is grounded in the current UI.'


def get_browser_automation_tool():
    return {
        'name': 'browser_automation',
        'description': (
            'Automates browser interactions using a headless browser (Playwright + Chromium). '
            'Navigate, fill forms, click, read text/links, upload files, and screenshot.\n\n'
            'HOW TO USE THIS TOOL WELL:\n'
            '1. ALWAYS start with a get_text (no selector) to understand what page you are on. Never guess selectors blind.\n'
            '2. Before filling any form, run inspect_forms. It returns every visible input/button/select with its real id, name, placeholder, aria-label, data-testid, text, AND a suggested_selector that is guaranteed to exist on the page. Use the suggested_selector verbatim in your fill/click actions — do NOT invent selectors like input[name="email"] and hope. This is especially critical on React/SPA sites where CSS classes are hashed and name attributes are often missing.\n'
            '3. Before clicking around on modern SPAs, run inspect_interactives. It returns visible buttons, links, clickable rows, and other interactive elements with text, aria labels, stable selectors, and click coordinates. Use this when the target is not a traditional form control, especially on Instagram, Gmail, and dashboard UIs.\n'
            '4. Sign in vs Sign up: these look almost identical. Before filling anything, read the page heading and primary button text from get_text / inspect_forms. If the form is not the one you want (e.g. you need to create an account but landed on a login form), look at the get_links output for a link like "Create account" / "Sign up" / "New here?" and click that FIRST. Do not just start filling fields and hope.\n'
            '5. React / SPA pages: content renders AFTER the initial HTML loads, and clicks often do not trigger a page navigation — the page mutates in place. If an element is not there yet, add a wait_for_selector action targeting it, or a wait action of 1500-3000ms. After submitting a form on an SPA, use wait_for_selector for the next step rather than assuming it loaded. Re-run inspect_forms or inspect_interactives after the page mutates to get fresh selectors for the new state.\n'
            '6. If a selector click still fails but inspect_interactives found the right target, use click_at with the returned x and y coordinates. This is especially useful for weird clickable rows, overlays, and non-semantic div-based UIs.\n'
            '7. If a web task asks for an email address (signup, newsletter, form), use YOUR own email address (provided in the system prompt). Do not use the owner\'s email.\n'
            '8. The result always includes page_text_snippet and final_url so you can verify where you ended up before claiming success. If final_url still looks like the form page after a submit, the submit probably failed — inspect and try again.\n'
            '9. When a browser step fails, the tool STOPS immediately at that step (subsequent actions are NOT run) and returns success=false with a top-level blocked_reason (target_not_found, timeout, off_screen, auth_required, needs_human, unknown), the failing step, a recovery_hint, and a recovery_snapshot of nearby interactive elements, forms, and site-specific landmarks. Read the snapshot and pick a real target from there. On needs_human or auth_required, stop and ask the user — do not keep retrying. Do not resend the same action with a slightly different selector — the run will be aborted after 3 attempts on the same target.\n'
            '10. A final screenshot is saved to disk; use send_photo_user to forward it to the current user.\n'
            '11. For multi-step logged-in flows, pass a session_id. The tool will keep that browser session alive across calls so cookies and page state survive. On later calls, omit url if you want to continue on the current page without reloading.\n'
            '12. To upload a local file (for example a profile picture), use the set_input_files action with value set to the file path.\n'
            '13. For sites where you need to stay logged in across process restarts (Instagram, Gmail, etc.), pass auth_profile=<name>. On first use, log in normally — cookies and localStorage are saved to disk after the call. On every subsequent use with the same name, the browser launches already logged in. session_id and auth_profile are complementary: session_id keeps a browser alive in memory across back-to-back calls; auth_profile persists login state to disk across everything.\n'
            "14. For Instagram work, call instagram_session first, use auth_profile='instagram', prefer direct URLs for inbox/notifications/profile pages, and use inspect_interactives for DM threads and modal controls."
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'url': {
                    'type': 'string',
                    'description': 'The URL to navigate to. Optional when resuming an existing session_id and continuing on the current page.'
                },
                'actions': {
                    'type': 'array',
                    'description': 'List of actions to perform in order',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'type': {
                                'type': 'string',
                                'description': (
                                    'Action type. '
                                    'fill sets a field value directly (fast, but may not trigger React state updates). '
                                    'type simulates keystrokes character-by-character (slower, but works reliably with React/SPA controlled components). '
                                    'Use type instead of fill when a form submission seems to ignore filled values. '
                                    'click/select/wait/screenshot/scroll interact with the page. click_at performs a real mouse click at explicit x/y coordinates. screenshot can take an optional selector to capture just that element instead of the full viewport. '
                                    'set_input_files uploads a local file into an <input type="file"> using the file path in value. '
                                    'wait_for_selector pauses until an element appears (essential for React/SPA pages after clicks). '
                                    'get_text returns visible text (whole page, or scoped to selector). '
                                    'get_links returns all <a> tags with their text + href (including mailto:). '
                                    'inspect_forms returns every visible input/button/select with its real attributes AND a suggested_selector — use this BEFORE filling anything on a React/SPA page so you stop guessing selectors. '
                                    'inspect_interactives returns visible clickable elements, including non-form controls like links, rows, tabs, modal actions, and div-based buttons.'
                                ),
                                'enum': [
                                    'fill',
                                    'type',
                                    'click',
                                    'click_at',
                                    'select',
                                    'set_input_files',
                                    'wait',
                                    'wait_for_selector',
                                    'screenshot',
                                    'scroll',
                                    'get_text',
                                    'get_links',
                                    'inspect_forms',
                                    'inspect_interactives',
                                ]
                            },
                            'selector': {
                                'type': 'string',
                                'description': 'Playwright selector for the element (e.g. input[name="email"], #submit-btn, button:has-text("Subscribe"), text=Next). Optional for get_text/get_links (omit to scope to whole page).'
                            },
                            'value': {
                                'type': 'string',
                                'description': 'Value to fill in, type, select, or upload from disk depending on the action'
                            },
                            'timeout': {
                                'type': 'number',
                                'description': 'Timeout in ms for wait action (default 5000)'
                            },
                            'x': {
                                'type': 'number',
                                'description': 'Viewport x coordinate for click_at. Usually copied from inspect_interactives.'
                            },
                            'y': {
                                'type': 'number',
                                'description': 'Viewport y coordinate for click_at. Usually copied from inspect_interactives.'
                            }
                        },
                        'required': ['type']
                    }
                },
                'wait_for_navigation': {
                    'type': 'boolean',
                    'description': 'Wait for page navigation after last action (default false)'
                },
                'session_id': {
                    'type': 'string',
                    'description': 'Optional browser session key. If provided, the same browser context/page will be reused across calls until it is closed or idles out.'
                },
                'close_session': {
                    'type': 'boolean',
                    'description': 'If true and session_id is set, close that persistent session after the actions finish.'
                },
                'auth_profile': {
                    'type': 'string',
                    'description': 'Optional persistent auth profile name (e.g. "instagram", "gmail"). Loads saved cookies/localStorage from ~/.lumakit/browser_profiles/<name>.json at launch and saves fresh state back after every successful call. Use this whenever you need to stay logged in across process restarts.'
                },
                'screenshot': {
                    'type': 'boolean',
                    'description': 'Take a screenshot of the final page state (default false). Set to true only when you explicitly want to show the user what the page looks like.'
                },
                'timeout': {
                    'type': 'number',
                    'description': 'Overall timeout in seconds for the entire automation (default 30)'
                }
            }
        },
        'execute': _browser_automation
    }


def _page_text_snippet(page, limit=1500):
    """Grab a compact snippet of visible page text so the LLM has context."""
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ''
    if not text:
        return ''
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    compact = '\n'.join(lines)
    if len(compact) > limit:
        compact = compact[:limit] + '...'
    return compact


def _extract_links(page, selector=None):
    """Return all <a> tags as {text, href}. If selector given, scope to that container."""
    js = """
    (sel) => {
        const root = sel ? document.querySelector(sel) : document;
        if (!root) return [];
        const anchors = root.querySelectorAll('a[href]');
        const out = [];
        anchors.forEach(a => {
            const text = (a.innerText || a.textContent || '').trim();
            out.push({text: text.slice(0, 200), href: a.href});
        });
        return out;
    }
    """
    try:
        return page.evaluate(js, selector)
    except Exception:
        return []


def _inspect_forms(page, selector=None):
    """
    Return structured info about interactive elements (inputs, textareas, selects,
    buttons) with all addressable attributes AND a suggested_selector that is
    guaranteed to exist on the page. Essential for React/SPA sites where CSS
    classes are hashed and 'name' attributes are often absent.
    """
    js = r"""
    (sel) => {
        const root = sel ? document.querySelector(sel) : document;
        if (!root) return [];
        const nodes = root.querySelectorAll(
            'input, textarea, select, button, [role="button"], [role="textbox"]'
        );
        const esc = (v) => String(v).replace(/'/g, "\\'");
        const pick = (el) => {
            const testid = el.getAttribute('data-testid');
            if (testid) return `[data-testid='${esc(testid)}']`;
            if (el.id) return `#${CSS.escape(el.id)}`;
            const name = el.getAttribute('name');
            if (name) return `${el.tagName.toLowerCase()}[name='${esc(name)}']`;
            const aria = el.getAttribute('aria-label');
            if (aria) return `${el.tagName.toLowerCase()}[aria-label='${esc(aria)}']`;
            const placeholder = el.getAttribute('placeholder');
            if (placeholder) return `${el.tagName.toLowerCase()}[placeholder='${esc(placeholder)}']`;
            if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') {
                const txt = (el.innerText || el.textContent || '').trim();
                if (txt) return `${el.tagName.toLowerCase()}:has-text('${esc(txt.slice(0, 40))}')`;
            }
            const t = el.getAttribute('type');
            return t ? `${el.tagName.toLowerCase()}[type='${esc(t)}']` : el.tagName.toLowerCase();
        };
        const out = [];
        nodes.forEach((el) => {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;

            const entry = {
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || null,
                id: el.id || null,
                name: el.getAttribute('name') || null,
                placeholder: el.getAttribute('placeholder') || null,
                aria_label: el.getAttribute('aria-label') || null,
                data_testid: el.getAttribute('data-testid') || null,
                text: ((el.innerText || el.textContent || '').trim() || null),
                required: el.hasAttribute('required') || null,
                suggested_selector: pick(el),
            };
            if (entry.text && entry.text.length > 80) {
                entry.text = entry.text.slice(0, 80) + '...';
            }
            out.push(entry);
        });
        return out;
    }
    """
    try:
        return page.evaluate(js, selector)
    except Exception as e:
        return [{'error': f'inspect_forms failed: {e}'}]


def _inspect_interactives(page, selector=None, *, site_name: str = 'generic', limit: int = 30):
    """Return visible clickable elements with selectors and coordinate hints."""
    js = r"""
    ({ selector, siteName, limit }) => {
        const root = selector ? document.querySelector(selector) : document;
        if (!root) return [];

        const baseSelectors = [
            'a[href]',
            'button',
            'input[type="button"]',
            'input[type="submit"]',
            '[role="button"]',
            '[role="link"]',
            '[tabindex]:not([tabindex="-1"])',
            '[contenteditable="true"]',
            '[data-testid]',
            '[aria-label]',
            'summary',
        ];
        if (siteName === 'instagram') {
            baseSelectors.push(
                'div[role="button"]',
                'div[tabindex="0"]',
                'svg[aria-label]',
                'div[aria-label]',
                'span[role="button"]'
            );
        }

        const esc = (v) => String(v).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
        const cleanText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const truncate = (value, max = 80) => {
            const text = cleanText(value);
            return text.length > max ? text.slice(0, max) + '...' : text;
        };
        const cssPath = (el) => {
            if (!(el instanceof Element)) return null;
            const parts = [];
            let node = el;
            while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
                let part = node.tagName.toLowerCase();
                if (node.id) {
                    part += `#${CSS.escape(node.id)}`;
                    parts.unshift(part);
                    return parts.join(' > ');
                }
                const parent = node.parentElement;
                if (!parent) {
                    parts.unshift(part);
                    break;
                }
                const siblings = Array.from(parent.children).filter(
                    child => child.tagName === node.tagName
                );
                if (siblings.length > 1) {
                    part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
                }
                parts.unshift(part);
                node = parent;
            }
            return parts.join(' > ');
        };
        const pick = (el) => {
            const href = el.getAttribute('href');
            if (href) return `a[href='${esc(href)}']`;
            const testid = el.getAttribute('data-testid');
            if (testid) return `[data-testid='${esc(testid)}']`;
            if (el.id) return `#${CSS.escape(el.id)}`;
            const aria = cleanText(el.getAttribute('aria-label') || '');
            if (aria) return `[aria-label='${esc(aria)}']`;
            const role = cleanText(el.getAttribute('role') || '');
            const text = cleanText(el.innerText || el.textContent || '');
            if (role === 'button' && text) return `${el.tagName.toLowerCase()}[role='button']:has-text('${esc(text.slice(0, 40))}')`;
            if (text) return `text=${text.slice(0, 80)}`;
            return cssPath(el);
        };
        const seen = new Set();
        const elements = [];
        const nodes = root.querySelectorAll(baseSelectors.join(', '));

        for (const el of nodes) {
            if (!(el instanceof Element)) continue;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const rect = el.getBoundingClientRect();
            if (rect.width < 2 || rect.height < 2) continue;

            const tag = el.tagName.toLowerCase();
            const text = cleanText(el.innerText || el.textContent || '');
            const aria = cleanText(el.getAttribute('aria-label') || '');
            const href = el.getAttribute('href') || null;
            const role = el.getAttribute('role') || null;
            const cursor = style.cursor || '';
            const clickable = Boolean(
                href ||
                tag === 'button' ||
                tag === 'summary' ||
                role === 'button' ||
                role === 'link' ||
                el.getAttribute('contenteditable') === 'true' ||
                typeof el.onclick === 'function' ||
                (el.getAttribute('tabindex') !== null && el.getAttribute('tabindex') !== '-1') ||
                cursor === 'pointer' ||
                el.getAttribute('data-testid')
            );
            if (!clickable) continue;

            const suggestedSelector = pick(el);
            const key = `${tag}|${suggestedSelector}|${truncate(text, 50)}|${Math.round(rect.top)}`;
            if (!suggestedSelector || seen.has(key)) continue;
            seen.add(key);

            elements.push({
                tag,
                role,
                text: truncate(text),
                aria_label: truncate(aria),
                href,
                data_testid: el.getAttribute('data-testid') || null,
                suggested_selector: suggestedSelector,
                css_path: cssPath(el),
                x: Math.round(rect.left + (rect.width / 2)),
                y: Math.round(rect.top + (rect.height / 2)),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                needs_coordinate_click: Boolean(
                    !href && tag === 'div' && !role && !el.getAttribute('data-testid')
                ),
            });

            if (elements.length >= limit) break;
        }

        return elements;
    }
    """
    try:
        return page.evaluate(
            js,
            {'selector': selector, 'siteName': site_name, 'limit': max(1, int(limit))},
        )
    except Exception as exc:
        return [{'error': f'inspect_interactives failed: {exc}'}]


def _extract_text(page, selector=None, limit=4000):
    """Return visible text for the whole page or a selector."""
    try:
        if selector:
            text = page.evaluate(
                "(sel) => { const el = document.querySelector(sel); return el ? el.innerText : ''; }",
                selector,
            )
        else:
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception as e:
        return f'[error extracting text: {e}]'
    if not text:
        return ''
    if len(text) > limit:
        text = text[:limit] + '...'
    return text


def _capture_recovery_snapshot(page, *, site_name: str = 'generic') -> dict:
    snapshot = {
        'url': page.url,
        'title': page.title(),
        'page_text_snippet': _page_text_snippet(page, limit=1000),
    }
    interactive_elements = _inspect_interactives(page, site_name=site_name, limit=8)
    if interactive_elements:
        snapshot['interactive_elements'] = interactive_elements
    forms = _inspect_forms(page)
    if isinstance(forms, list) and forms:
        snapshot['forms'] = forms[:6]
    landmarks = landmark_selectors(site_name)
    if landmarks:
        snapshot['landmarks'] = landmarks
    return snapshot


def _browser_automation(inputs):
    url = inputs.get('url')
    actions = inputs.get('actions', [])
    wait_for_navigation = inputs.get('wait_for_navigation', False)
    session_id = inputs.get('session_id')
    close_session = bool(inputs.get('close_session', False))
    take_screenshot = bool(inputs.get('screenshot', False))
    overall_timeout = inputs.get('timeout', 30) * 1000
    auth_profile = (inputs.get('auth_profile') or '').strip() or None
    storage_state_path = _browser_profile_path(auth_profile) if auth_profile else None

    results = {
        'url': url,
        'actions_performed': [],
        'success': False,
    }

    if not url and not session_id:
        results['error'] = (
            'browser_automation requires a url unless you are resuming an existing '
            'session_id.'
        )
        return results

    if close_session and not session_id:
        results['error'] = 'close_session requires session_id.'
        return results

    browser = None
    context = None
    page = None
    site_name = 'generic'

    try:
        raise_if_interrupted("Browser automation interrupted by /stop.")

        if session_id:
            session, created = _get_or_create_browser_session(
                session_id, overall_timeout, storage_state_path=storage_state_path
            )
            browser = session['browser']
            context = session['context']
            page = session['page']
            # Use the profile bound to the session at creation time so we keep
            # saving to the same file even if later calls omit auth_profile.
            storage_state_path = session.get('storage_state_path') or storage_state_path
            results['session_id'] = session_id
            results['session_reused'] = not created
        else:
            browser, context, page = _launch_browser_components(
                overall_timeout, storage_state_path=storage_state_path
            )

        if auth_profile:
            results['auth_profile'] = auth_profile
            results['auth_profile_loaded'] = bool(
                storage_state_path and storage_state_path.exists()
            )
            if results['auth_profile_loaded']:
                report_status(f"Loaded saved browser login state for {auth_profile}.")
            else:
                report_status(f"Starting a fresh browser profile for {auth_profile}.")

        if url:
            raise_if_interrupted("Browser automation interrupted by /stop.")
            report_status(f"Navigating to {url}.")
            page.goto(url, wait_until='domcontentloaded')
            site_name = _current_site_name(url, page.url)
            _maybe_settle_page(page, site_name, 'navigate')
            results['page_title'] = page.title()
        else:
            site_name = _current_site_name(page.url)
            results['page_title'] = page.title()
            results['resumed_current_page'] = True
            report_status("Continuing in the existing browser session.")
            _maybe_settle_page(page, site_name, 'default')
        results['site'] = site_name

        for i, action in enumerate(actions):
            raise_if_interrupted("Browser automation interrupted by /stop.")
            action_type = action['type']
            action_result = {'step': i + 1, 'type': action_type}
            report_status(_describe_action(action))

            try:
                if action_type == 'fill':
                    selector = action['selector']
                    value = action['value']
                    page.fill(selector, value)
                    action_result['selector'] = selector
                    action_result['value'] = '***'
                    action_result['status'] = 'filled'

                elif action_type == 'type':
                    selector = action['selector']
                    value = action['value']
                    locator = page.locator(selector).first
                    try:
                        locator.wait_for(state='attached', timeout=2500)
                    except Exception:
                        pass
                    try:
                        locator.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    locator.click(timeout=5000)
                    page.keyboard.type(value, delay=50)
                    action_result['selector'] = selector
                    action_result['value'] = '***'
                    action_result['status'] = 'typed'

                elif action_type == 'click':
                    selector = action['selector']
                    click_meta = _click_with_retries(
                        page,
                        selector,
                        wait_for_navigation=bool(wait_for_navigation and i == len(actions) - 1),
                    )
                    action_result['selector'] = selector
                    action_result['status'] = 'clicked'
                    action_result.update(click_meta)

                elif action_type == 'click_at':
                    if action.get('x') is None or action.get('y') is None:
                        raise ValueError('click_at requires numeric x and y values.')
                    x = float(action['x'])
                    y = float(action['y'])
                    page.mouse.click(x, y)
                    if wait_for_navigation and i == len(actions) - 1:
                        _wait_for_optional_navigation(page)
                    action_result['x'] = round(x, 1)
                    action_result['y'] = round(y, 1)
                    action_result['status'] = 'clicked'
                    action_result['method'] = 'mouse.click'

                elif action_type == 'select':
                    selector = action['selector']
                    value = action['value']
                    page.select_option(selector, value)
                    action_result['selector'] = selector
                    action_result['status'] = 'selected'

                elif action_type == 'set_input_files':
                    selector = action['selector']
                    upload_path = _resolve_upload_path(action['value'])
                    page.set_input_files(selector, str(upload_path))
                    action_result['selector'] = selector
                    action_result['file_path'] = str(upload_path)
                    action_result['status'] = 'files_set'

                elif action_type == 'wait':
                    timeout = action.get('timeout', 5000)
                    _interruptible_wait(page, timeout)
                    action_result['status'] = 'waited'

                elif action_type == 'wait_for_selector':
                    selector = action['selector']
                    timeout = action.get('timeout', 10000)
                    _interruptible_wait_for_selector(page, selector, timeout)
                    action_result['selector'] = selector
                    action_result['status'] = 'selector_visible'

                elif action_type == 'screenshot':
                    ss_name = action.get('value', f'screenshot_step_{i + 1}.png')
                    ss_path = str(_screenshots_dir() / Path(ss_name).name)
                    page.screenshot(path=ss_path)
                    action_result['screenshot_path'] = ss_path
                    action_result['status'] = 'screenshot_taken'

                elif action_type == 'scroll':
                    page.keyboard.press('PageDown')
                    action_result['status'] = 'scrolled'

                elif action_type == 'get_text':
                    selector = action.get('selector')
                    text = _extract_text(page, selector=selector)
                    action_result['selector'] = selector
                    action_result['text'] = text
                    action_result['status'] = 'text_extracted'

                elif action_type == 'get_links':
                    selector = action.get('selector')
                    links = _extract_links(page, selector=selector)
                    action_result['selector'] = selector
                    action_result['links'] = links
                    action_result['count'] = len(links)
                    action_result['status'] = 'links_extracted'

                elif action_type == 'inspect_forms':
                    selector = action.get('selector')
                    elements = _inspect_forms(page, selector=selector)
                    action_result['selector'] = selector
                    action_result['elements'] = elements
                    action_result['count'] = len(elements)
                    action_result['status'] = 'forms_inspected'

                elif action_type == 'inspect_interactives':
                    selector = action.get('selector')
                    elements = _inspect_interactives(page, selector=selector, site_name=site_name)
                    action_result['selector'] = selector
                    action_result['elements'] = elements
                    action_result['count'] = len(elements)
                    landmarks = landmark_selectors(site_name)
                    if landmarks:
                        action_result['landmarks'] = landmarks
                    action_result['status'] = 'interactives_inspected'

            except OperationInterrupted:
                raise
            except Exception as e:
                blocked_reason, classifier_hint = _classify_failure(action, str(e), page)
                action_result['status'] = 'failed'
                action_result['error'] = str(e)
                action_result['blocked_reason'] = blocked_reason
                action_result['recovery_hint'] = (
                    classifier_hint or _build_recovery_hint(action_type, site_name)
                )
                action_result['recovery_snapshot'] = _capture_recovery_snapshot(
                    page, site_name=site_name
                )
                report_status(
                    f"{action_type.replace('_', ' ').capitalize()} failed"
                    + (
                        f" on {_short_selector(action.get('selector'))}"
                        if action.get('selector')
                        else ""
                    )
                    + f": {e}"
                )
                results['actions_performed'].append(action_result)
                # Hard-stop: do not run subsequent actions on a page in an
                # unknown state. The model gets a clean error + snapshot and
                # can decide the next step with fresh context.
                results['blocked_reason'] = blocked_reason
                results['blocked_on_step'] = action_result['step']
                results['skipped_remaining_actions'] = max(
                    0, len(actions) - (i + 1)
                )
                break

            site_name = _current_site_name(page.url, url)
            results['site'] = site_name
            if action_type in {'click', 'click_at', 'type', 'set_input_files'}:
                _maybe_settle_page(page, site_name, action_type)
            results['actions_performed'].append(action_result)

        _interruptible_wait(page, 1000)
        raise_if_interrupted("Browser automation interrupted by /stop.")
        results['page_text_snippet'] = _page_text_snippet(page)
        results['page_observation'] = _capture_recovery_snapshot(page, site_name=site_name)

        if take_screenshot:
            raise_if_interrupted("Browser automation interrupted by /stop.")
            final_screenshot = str(_screenshots_dir() / 'browser_automation_result.png')
            page.screenshot(path=final_screenshot, full_page=False)
            results['screenshot_path'] = final_screenshot

        results['final_url'] = page.url
        results['final_title'] = page.title()
        failed_actions = [
            action for action in results['actions_performed']
            if isinstance(action, dict) and action.get('status') == 'failed'
        ]
        results['failed_action_count'] = len(failed_actions)
        results['completed_with_failures'] = bool(failed_actions)
        results['success'] = not failed_actions

        _OBSERVATION_ACTIONS = {'get_text', 'get_links', 'inspect_forms', 'inspect_interactives', 'screenshot'}
        mutating_done = [
            a for a in results['actions_performed']
            if isinstance(a, dict)
            and a.get('status') not in {'failed', None}
            and a.get('type') not in _OBSERVATION_ACTIONS
        ]
        results['no_progress'] = not mutating_done and not failed_actions

        if failed_actions:
            first_failure = failed_actions[0]
            target = first_failure.get('selector') or first_failure.get('type') or 'browser step'
            reason = results.get('blocked_reason') or first_failure.get('blocked_reason') or 'unknown'
            results['error'] = (
                f"Browser step blocked ({reason}) at step {first_failure.get('step')}. "
                f"Target: {target}. {first_failure.get('recovery_hint', '')}"
            ).strip()
            report_status(f"Browser step blocked ({reason}) at {results['final_url']}.")
        elif results['no_progress']:
            report_status(f"Observed {results['final_url']} (no changes made).")
        else:
            report_status(f"Browser step finished on {results['final_url']}.")

        if storage_state_path is not None:
            results['auth_profile_saved'] = _save_storage_state(context, storage_state_path)

        if session_id:
            with _SESSIONS_LOCK:
                live_session = _BROWSER_SESSIONS.get(session_id)
                if live_session is not None:
                    live_session['last_used'] = time.time()
            results['session_kept_alive'] = not close_session

    except OperationInterrupted:
        raise
    except Exception as e:
        results['error'] = str(e)
        report_status(f"Browser automation failed: {e}")
    finally:
        if session_id and close_session:
            with _SESSIONS_LOCK:
                results['session_closed'] = _close_browser_session_locked(session_id)
        elif session_id:
            _cleanup_stale_sessions()
        else:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

        with _SESSIONS_LOCK:
            _stop_playwright_if_idle_locked()

    return results
