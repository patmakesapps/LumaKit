"""
Browser automation tool using Playwright.
Supports navigating to URLs, filling form fields, clicking buttons,
reading page text/links, uploading files, and submitting forms.
"""

import os
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from core.paths import get_repo_root


# Don't launch Chromium if the machine is already under obvious memory pressure.
_MIN_AVAILABLE_RAM = 1_000_000_000  # ~1 GiB
_SESSION_IDLE_TIMEOUT_SECONDS = 20 * 60

_PLAYWRIGHT = None
_BROWSER_SESSIONS = {}
_SESSIONS_LOCK = threading.RLock()


def _screenshots_dir() -> Path:
    """Return the repo's screenshots/ folder, creating it if needed."""
    path = get_repo_root() / "screenshots"
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


def _ensure_playwright_started():
    """Start Playwright lazily so sessions can survive across tool calls."""
    global _PLAYWRIGHT
    with _SESSIONS_LOCK:
        if _PLAYWRIGHT is None:
            _PLAYWRIGHT = sync_playwright().start()
        return _PLAYWRIGHT


def _launch_browser_components(overall_timeout):
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
    context = browser.new_context(
        viewport={'width': 1280, 'height': 720},
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
    )
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


def _get_or_create_browser_session(session_id, overall_timeout):
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

        browser, context, page = _launch_browser_components(overall_timeout)
        session = {
            'browser': browser,
            'context': context,
            'page': page,
            'created_at': now,
            'last_used': now,
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


def get_browser_automation_tool():
    return {
        'name': 'browser_automation',
        'description': (
            'Automates browser interactions using a headless browser (Playwright + Chromium). '
            'Navigate, fill forms, click, read text/links, upload files, and screenshot.\n\n'
            'HOW TO USE THIS TOOL WELL:\n'
            '1. ALWAYS start with a get_text (no selector) to understand what page you are on. Never guess selectors blind.\n'
            '2. Before filling any form, run inspect_forms. It returns every visible input/button/select with its real id, name, placeholder, aria-label, data-testid, text, AND a suggested_selector that is guaranteed to exist on the page. Use the suggested_selector verbatim in your fill/click actions — do NOT invent selectors like input[name="email"] and hope. This is especially critical on React/SPA sites where CSS classes are hashed and name attributes are often missing.\n'
            '3. Sign in vs Sign up: these look almost identical. Before filling anything, read the page heading and primary button text from get_text / inspect_forms. If the form is not the one you want (e.g. you need to create an account but landed on a login form), look at the get_links output for a link like "Create account" / "Sign up" / "New here?" and click that FIRST. Do not just start filling fields and hope.\n'
            '4. React / SPA pages: content renders AFTER the initial HTML loads, and clicks often do not trigger a page navigation — the page mutates in place. If an element is not there yet, add a wait_for_selector action targeting it, or a wait action of 1500-3000ms. After submitting a form on an SPA, use wait_for_selector for the next step rather than assuming it loaded. Re-run inspect_forms after the page mutates to get fresh selectors for the new state.\n'
            '5. If a web task asks for an email address (signup, newsletter, form), use YOUR own email address (provided in the system prompt). Do not use the owner\'s email.\n'
            '6. The result always includes page_text_snippet and final_url so you can verify where you ended up before claiming success. If final_url still looks like the form page after a submit, the submit probably failed — inspect and try again.\n'
            '7. A final screenshot is saved to disk; use send_photo_telegram to forward it to the user.\n'
            '8. For multi-step logged-in flows, pass a session_id. The tool will keep that browser session alive across calls so cookies and page state survive. On later calls, omit url if you want to continue on the current page without reloading.\n'
            '9. To upload a local file (for example a profile picture), use the set_input_files action with value set to the file path.'
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
                                    'click/select/wait/screenshot/scroll interact with the page. screenshot can take an optional selector to capture just that element instead of the full viewport. '
                                    'set_input_files uploads a local file into an <input type="file"> using the file path in value. '
                                    'wait_for_selector pauses until an element appears (essential for React/SPA pages after clicks). '
                                    'get_text returns visible text (whole page, or scoped to selector). '
                                    'get_links returns all <a> tags with their text + href (including mailto:). '
                                    'inspect_forms returns every visible input/button/select with its real attributes AND a suggested_selector — use this BEFORE filling anything on a React/SPA page so you stop guessing selectors.'
                                ),
                                'enum': [
                                    'fill',
                                    'type',
                                    'click',
                                    'select',
                                    'set_input_files',
                                    'wait',
                                    'wait_for_selector',
                                    'screenshot',
                                    'scroll',
                                    'get_text',
                                    'get_links',
                                    'inspect_forms',
                                ]
                            },
                            'selector': {
                                'type': 'string',
                                'description': 'CSS selector for the element (e.g. input[name="email"], #submit-btn, button:has-text("Subscribe")). Optional for get_text/get_links (omit to scope to whole page).'
                            },
                            'value': {
                                'type': 'string',
                                'description': 'Value to fill in, type, select, or upload from disk depending on the action'
                            },
                            'timeout': {
                                'type': 'number',
                                'description': 'Timeout in ms for wait action (default 5000)'
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
                'screenshot': {
                    'type': 'boolean',
                    'description': 'Take a screenshot of the final page state (default true)'
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


def _browser_automation(inputs):
    url = inputs.get('url')
    actions = inputs.get('actions', [])
    wait_for_navigation = inputs.get('wait_for_navigation', False)
    session_id = inputs.get('session_id')
    close_session = bool(inputs.get('close_session', False))
    take_screenshot = inputs.get('screenshot', True)
    overall_timeout = inputs.get('timeout', 30) * 1000

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

    try:
        if session_id:
            session, created = _get_or_create_browser_session(session_id, overall_timeout)
            browser = session['browser']
            context = session['context']
            page = session['page']
            results['session_id'] = session_id
            results['session_reused'] = not created
        else:
            browser, context, page = _launch_browser_components(overall_timeout)

        if url:
            page.goto(url, wait_until='domcontentloaded')
            results['page_title'] = page.title()
        else:
            results['page_title'] = page.title()
            results['resumed_current_page'] = True

        for i, action in enumerate(actions):
            action_type = action['type']
            action_result = {'step': i + 1, 'type': action_type}

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
                    page.click(selector)
                    page.keyboard.type(value, delay=50)
                    action_result['selector'] = selector
                    action_result['value'] = '***'
                    action_result['status'] = 'typed'

                elif action_type == 'click':
                    selector = action['selector']
                    if wait_for_navigation and i == len(actions) - 1:
                        page.click(selector)
                        page.wait_for_load_state('networkidle')
                    else:
                        page.click(selector)
                    action_result['selector'] = selector
                    action_result['status'] = 'clicked'

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
                    page.wait_for_timeout(timeout)
                    action_result['status'] = 'waited'

                elif action_type == 'wait_for_selector':
                    selector = action['selector']
                    timeout = action.get('timeout', 10000)
                    page.wait_for_selector(selector, timeout=timeout, state='visible')
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

            except Exception as e:
                action_result['status'] = 'failed'
                action_result['error'] = str(e)
                results['actions_performed'].append(action_result)
                continue

            results['actions_performed'].append(action_result)

        page.wait_for_timeout(1000)
        results['page_text_snippet'] = _page_text_snippet(page)

        if take_screenshot:
            final_screenshot = str(_screenshots_dir() / 'browser_automation_result.png')
            page.screenshot(path=final_screenshot, full_page=False)
            results['screenshot_path'] = final_screenshot

        results['final_url'] = page.url
        results['final_title'] = page.title()
        results['success'] = True

        if session_id:
            with _SESSIONS_LOCK:
                live_session = _BROWSER_SESSIONS.get(session_id)
                if live_session is not None:
                    live_session['last_used'] = time.time()
            results['session_kept_alive'] = not close_session

    except Exception as e:
        results['error'] = str(e)
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

    return results
