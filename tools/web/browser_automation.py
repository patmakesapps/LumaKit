"""
Browser automation tool using Playwright.
Supports navigating to URLs, filling form fields, clicking buttons,
reading page text/links, and submitting forms.
"""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from core.paths import get_repo_root


def _screenshots_dir() -> Path:
    """Return the repo's screenshots/ folder, creating it if needed."""
    path = get_repo_root() / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_browser_automation_tool():
    return {
        'name': 'browser_automation',
        'description': (
            'Automates browser interactions using a headless browser (Playwright + Chromium). '
            'Navigate, fill forms, click, read text/links, and screenshot.\n\n'
            'HOW TO USE THIS TOOL WELL:\n'
            '1. ALWAYS start with a get_text (no selector) to understand what page you are on. Never guess selectors blind.\n'
            '2. Before filling any form, run inspect_forms. It returns every visible input/button/select with its real id, name, placeholder, aria-label, data-testid, text, AND a suggested_selector that is guaranteed to exist on the page. Use the suggested_selector verbatim in your fill/click actions — do NOT invent selectors like input[name="email"] and hope. This is especially critical on React/SPA sites where CSS classes are hashed and name attributes are often missing.\n'
            '3. Sign in vs Sign up: these look almost identical. Before filling anything, read the page heading and primary button text from get_text / inspect_forms. If the form is not the one you want (e.g. you need to create an account but landed on a login form), look at the get_links output for a link like "Create account" / "Sign up" / "New here?" and click that FIRST. Do not just start filling fields and hope.\n'
            '4. React / SPA pages: content renders AFTER the initial HTML loads, and clicks often do not trigger a page navigation — the page mutates in place. If an element is not there yet, add a wait_for_selector action targeting it, or a wait action of 1500-3000ms. After submitting a form on an SPA, use wait_for_selector for the next step rather than assuming it loaded. Re-run inspect_forms after the page mutates to get fresh selectors for the new state.\n'
            '5. If a web task asks for an email address (signup, newsletter, form), use YOUR own email address (provided in the system prompt). Do not use the owner\'s email.\n'
            '6. The result always includes page_text_snippet and final_url so you can verify where you ended up before claiming success. If final_url still looks like the form page after a submit, the submit probably failed — inspect and try again.\n'
            '7. A final screenshot is saved to disk; use send_photo_telegram to forward it to the user.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'url': {
                    'type': 'string',
                    'description': 'The URL to navigate to'
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
                                    'wait_for_selector pauses until an element appears (essential for React/SPA pages after clicks). '
                                    'get_text returns visible text (whole page, or scoped to selector). '
                                    'get_links returns all <a> tags with their text + href (including mailto:). '
                                    'inspect_forms returns every visible input/button/select with its real attributes AND a suggested_selector — use this BEFORE filling anything on a React/SPA page so you stop guessing selectors.'
                                ),
                                'enum': ['fill', 'type', 'click', 'select', 'wait', 'wait_for_selector', 'screenshot', 'scroll', 'get_text', 'get_links', 'inspect_forms']
                            },
                            'selector': {
                                'type': 'string',
                                'description': 'CSS selector for the element (e.g. input[name="email"], #submit-btn, button:has-text("Subscribe")). Optional for get_text/get_links (omit to scope to whole page).'
                            },
                            'value': {
                                'type': 'string',
                                'description': 'Value to fill in or type (for fill/type/select actions)'
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
                'screenshot': {
                    'type': 'boolean',
                    'description': 'Take a screenshot of the final page state (default true)'
                },
                'timeout': {
                    'type': 'number',
                    'description': 'Overall timeout in seconds for the entire automation (default 30)'
                }
            },
            'required': ['url']
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
    # Collapse whitespace to keep the snippet short
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
            // Priority order for a selector that uniquely (or usefully) targets el.
            const testid = el.getAttribute('data-testid');
            if (testid) return `[data-testid='${esc(testid)}']`;
            if (el.id) return `#${CSS.escape(el.id)}`;
            const name = el.getAttribute('name');
            if (name) return `${el.tagName.toLowerCase()}[name='${esc(name)}']`;
            const aria = el.getAttribute('aria-label');
            if (aria) return `${el.tagName.toLowerCase()}[aria-label='${esc(aria)}']`;
            const placeholder = el.getAttribute('placeholder');
            if (placeholder) return `${el.tagName.toLowerCase()}[placeholder='${esc(placeholder)}']`;
            // Buttons: prefer visible text
            if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') {
                const txt = (el.innerText || el.textContent || '').trim();
                if (txt) return `${el.tagName.toLowerCase()}:has-text('${esc(txt.slice(0, 40))}')`;
            }
            // Fallback: tag + type
            const t = el.getAttribute('type');
            return t ? `${el.tagName.toLowerCase()}[type='${esc(t)}']` : el.tagName.toLowerCase();
        };
        const out = [];
        nodes.forEach((el) => {
            // Skip hidden elements — they can't be interacted with anyway
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
            // Trim long text
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
    url = inputs['url']
    actions = inputs.get('actions', [])
    wait_for_navigation = inputs.get('wait_for_navigation', False)
    take_screenshot = inputs.get('screenshot', True)
    overall_timeout = inputs.get('timeout', 30) * 1000  # convert to ms

    results = {
        'url': url,
        'actions_performed': [],
        'success': False
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = context.new_page()
            page.set_default_timeout(overall_timeout)

            # Navigate to the URL. Use domcontentloaded — networkidle is unreliable
            # on modern sites (analytics, WebSockets, long-polling never settle)
            # and caused the agent to get confused by mid-flight redirects.
            # If a page needs extra hydration time, the LLM can add a wait or
            # wait_for_selector action explicitly.
            page.goto(url, wait_until='domcontentloaded')
            results['page_title'] = page.title()

            # Perform each action
            for i, action in enumerate(actions):
                action_type = action['type']
                action_result = {'step': i + 1, 'type': action_type}

                try:
                    if action_type == 'fill':
                        selector = action['selector']
                        value = action['value']
                        page.fill(selector, value)
                        action_result['selector'] = selector
                        action_result['value'] = '***'  # don't log sensitive values
                        action_result['status'] = 'filled'

                    elif action_type == 'type':
                        selector = action['selector']
                        value = action['value']
                        # Click to focus the field first, then type character-by-character
                        page.click(selector)
                        page.keyboard.type(value, delay=50)
                        action_result['selector'] = selector
                        action_result['value'] = '***'  # don't log sensitive values
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
                        # Always anchor the filename inside screenshots/, even
                        # if the model passes an absolute or nested path.
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
                    # Don't stop on failure — continue with next actions
                    continue

                results['actions_performed'].append(action_result)

            # Wait a moment for any final JS to run
            page.wait_for_timeout(1000)

            # Include a short page-text snippet so the LLM isn't flying blind
            results['page_text_snippet'] = _page_text_snippet(page)

            # Take final screenshot if requested
            if take_screenshot:
                final_screenshot = str(_screenshots_dir() / 'browser_automation_result.png')
                page.screenshot(path=final_screenshot, full_page=False)
                results['screenshot_path'] = final_screenshot

            # Capture final page state
            results['final_url'] = page.url
            results['final_title'] = page.title()
            results['success'] = True

            browser.close()

    except Exception as e:
        results['error'] = str(e)

    return results
