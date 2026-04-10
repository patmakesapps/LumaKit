"""
Browser automation tool using Playwright.
Supports navigating to URLs, filling form fields, clicking buttons,
and signing up for newsletters / submitting forms.
"""

import os
from playwright.sync_api import sync_playwright


def get_browser_automation_tool():
    return {
        'name': 'browser_automation',
        'description': (
            'Automates browser interactions using a headless browser. '
            'Can navigate to URLs, fill in form fields, click buttons, and submit forms. '
            'Useful for signing up for newsletters, filling out contact forms, etc.'
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
                                'description': 'Action type: fill, click, select, wait, screenshot, scroll',
                                'enum': ['fill', 'click', 'select', 'wait', 'screenshot', 'scroll']
                            },
                            'selector': {
                                'type': 'string',
                                'description': 'CSS selector for the element (e.g. input[name="email"], #submit-btn, button:has-text("Subscribe"))'
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
                        ss_path = action.get('value', f'screenshot_step_{i + 1}.png')
                        page.screenshot(path=ss_path)
                        action_result['screenshot_path'] = ss_path
                        action_result['status'] = 'screenshot_taken'

                    elif action_type == 'scroll':
                        page.keyboard.press('PageDown')
                        action_result['status'] = 'scrolled'

                except Exception as e:
                    action_result['status'] = 'failed'
                    action_result['error'] = str(e)
                    results['actions_performed'].append(action_result)
                    # Don't stop on failure — continue with next actions
                    continue

                results['actions_performed'].append(action_result)

            # Wait a moment for any final JS to run
            page.wait_for_timeout(1000)

            # Take final screenshot if requested
            if take_screenshot:
                final_screenshot = 'browser_automation_result.png'
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