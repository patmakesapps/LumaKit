"""
Browser automation tool using Playwright.
Supports navigating to URLs, filling form fields, clicking buttons,
reading page text/links, and submitting forms.
"""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def get_browser_automation_tool():
    return {
        'name': 'browser_automation',
        'description': (
            'Automates browser interactions using a headless browser. '
            'Can navigate to URLs, fill in form fields, click buttons, submit forms, '
            'and read page text or links. '
            'Use get_text / get_links actions to inspect a page before guessing CSS selectors. '
            'The result always includes a short page_text_snippet so you can see what loaded. '
            'A final screenshot is saved to disk; use send_photo_telegram to forward it to the user.'
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
                                    'fill/click/select/wait/screenshot/scroll interact with the page. '
                                    'get_text returns visible text (whole page, or scoped to selector). '
                                    'get_links returns all <a> tags with their text + href (including mailto:).'
                                ),
                                'enum': ['fill', 'click', 'select', 'wait', 'screenshot', 'scroll', 'get_text', 'get_links']
                            },
                            'selector': {
                                'type': 'string',
                                'description': 'CSS selector for the element (e.g. input[name="email"], #submit-btn, button:has-text("Subscribe")). Optional for get_text/get_links (omit to scope to whole page).'
                            },
                            'value': {
                                'type': 'string',
                                'description': 'Value to fill in (for fill/select actions)'
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

            # Navigate to the URL
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

                    elif action_type == 'screenshot':
                        ss_name = action.get('value', f'screenshot_step_{i + 1}.png')
                        ss_path = str(Path(ss_name).resolve())
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
                final_screenshot = str(Path('browser_automation_result.png').resolve())
                page.screenshot(path=final_screenshot, full_page=True)
                results['screenshot_path'] = final_screenshot

            # Capture final page state
            results['final_url'] = page.url
            results['final_title'] = page.title()
            results['success'] = True

            browser.close()

    except Exception as e:
        results['error'] = str(e)

    return results
