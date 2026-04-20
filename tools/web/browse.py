"""Intent-based browsing tool.

`browser_automation` is for when the caller knows the exact selectors and
steps. `browse` is for when the caller knows the *goal* — "send a DM to
jordan", "read the inbox" — and wants the tool to run its own
observe -> act -> verify loop against a live Playwright page.

Skills live in tools/web/skills/<site>.py. Each skill is a deterministic
handler that reads the real DOM between micro-steps instead of making the
LLM guess selectors across tool calls.
"""

from core.display import status as report_status
from core.interrupts import OperationInterrupted, raise_if_interrupted
from tools.web import browser_automation as _ba
from tools.web.skills import get_skill, list_skills


def get_browse_tool():
    return {
        'name': 'browse',
        'description': (
            'High-level browsing tool for goals where exact selectors are unknown '
            '(social media, webmail, dashboards). Unlike browser_automation — which '
            'takes a list of clicks/fills and fails on bad selectors — browse runs '
            'an internal observe -> act -> verify loop against a live page and '
            'returns one structured result.\n\n'
            'WHEN TO USE:\n'
            '- Use browse for Instagram/social DMs, inbox checks, posting comments, '
            'and similar flows where CSS classes are hashed and the model otherwise '
            'has to guess selectors blind.\n'
            '- Use browser_automation when you know the exact selectors (login '
            'forms, public sites with stable ids, structured pages).\n\n'
            'SUPPORTED SKILLS (site -> actions):\n'
            f'{_format_skills_for_description(list_skills())}\n\n'
            'RESULT SHAPE:\n'
            '  status: "done" | "blocked" | "needs_human"\n'
            '  narrative: one-line human summary of what actually happened\n'
            '  blocked_reason: target_not_found | auth_required | needs_human | '
            'timeout | unknown (only on non-done)\n'
            '  observations: dict of verified facts (final_url, matched_user, '
            'candidates, threads, etc.)\n'
            '  no_progress: true if the run did not mutate anything\n\n'
            'SESSIONS: pass session_id + auth_profile the same way as '
            'browser_automation so the logged-in browser persists across calls. '
            "For Instagram, use auth_profile='instagram' after running "
            'instagram_session once to log in.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'site': {
                    'type': 'string',
                    'description': 'Which site to operate on (e.g. "instagram").'
                },
                'action': {
                    'type': 'string',
                    'description': (
                        'Skill name. See SUPPORTED SKILLS in the description. '
                        'Example: "send_dm", "open_dm", "check_inbox".'
                    )
                },
                'target': {
                    'type': 'string',
                    'description': 'Target of the action when relevant (e.g. username for send_dm).'
                },
                'value': {
                    'type': 'string',
                    'description': 'Payload for the action (e.g. the message text for send_dm).'
                },
                'limit': {
                    'type': 'number',
                    'description': 'Optional cap for list-returning actions like check_inbox (default 10).'
                },
                'session_id': {
                    'type': 'string',
                    'description': 'Persistent browser session key. Same semantics as browser_automation.'
                },
                'auth_profile': {
                    'type': 'string',
                    'description': 'Persistent auth profile name (e.g. "instagram"). Loads/saves cookies across process restarts.'
                },
                'close_session': {
                    'type': 'boolean',
                    'description': 'If true and session_id is set, close the persistent session after the skill finishes.'
                },
                'timeout': {
                    'type': 'number',
                    'description': 'Overall timeout in seconds for this browse call (default 30).'
                }
            },
            'required': ['site', 'action']
        },
        'execute': _execute_browse,
    }


def _format_skills_for_description(skills: dict) -> str:
    lines = []
    for site, actions in sorted(skills.items()):
        lines.append(f"  {site}: {', '.join(actions)}")
    return '\n'.join(lines)


def _execute_browse(inputs: dict) -> dict:
    site = (inputs.get('site') or '').strip()
    action = (inputs.get('action') or '').strip()
    target = inputs.get('target') or ''
    value = inputs.get('value') or ''
    limit = inputs.get('limit')
    session_id = inputs.get('session_id')
    close_session = bool(inputs.get('close_session', False))
    auth_profile = (inputs.get('auth_profile') or '').strip() or None
    overall_timeout = (inputs.get('timeout') or 30) * 1000

    result: dict = {
        'site': site,
        'action': action,
        'status': 'blocked',
        'narrative': '',
        'no_progress': True,
    }

    handler = get_skill(site, action)
    if handler is None:
        result['blocked_reason'] = 'target_not_found'
        result['narrative'] = (
            f"No skill registered for {site}.{action}. "
            f"Known: {list_skills()}."
        )
        return result

    storage_state_path = _ba._browser_profile_path(auth_profile) if auth_profile else None

    browser = None
    context = None
    page = None
    session_created = False

    try:
        raise_if_interrupted("browse interrupted by /stop.")

        if session_id:
            session, session_created = _ba._get_or_create_browser_session(
                session_id, overall_timeout, storage_state_path=storage_state_path
            )
            browser = session['browser']
            context = session['context']
            page = session['page']
            storage_state_path = session.get('storage_state_path') or storage_state_path
            result['session_id'] = session_id
            result['session_reused'] = not session_created
        else:
            browser, context, page = _ba._launch_browser_components(
                overall_timeout, storage_state_path=storage_state_path
            )

        if auth_profile:
            result['auth_profile'] = auth_profile
            result['auth_profile_loaded'] = bool(
                storage_state_path and storage_state_path.exists()
            )

        report_status(f"Running {site}.{action} …")
        skill_result = handler(
            page,
            target=target,
            value=value,
            limit=limit,
        )

        result.update(skill_result)

        if storage_state_path is not None and context is not None:
            result['auth_profile_saved'] = _ba._save_storage_state(context, storage_state_path)

        narrative = result.get('narrative') or f"{site}.{action} finished."
        status = result.get('status', 'blocked')
        if status == 'done':
            report_status(narrative)
        else:
            reason = result.get('blocked_reason', 'unknown')
            report_status(f"{site}.{action} blocked ({reason}): {narrative}")

    except OperationInterrupted:
        raise
    except Exception as exc:
        result['status'] = 'blocked'
        result['blocked_reason'] = 'unknown'
        result['narrative'] = f"{site}.{action} crashed: {exc}"
        result['error'] = str(exc)
        report_status(result['narrative'])
    finally:
        if session_id and close_session:
            import threading  # noqa: F401 — lock lives in browser_automation
            with _ba._SESSIONS_LOCK:
                result['session_closed'] = _ba._close_browser_session_locked(session_id)
        elif session_id:
            _ba._cleanup_stale_sessions()
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

        with _ba._SESSIONS_LOCK:
            _ba._stop_playwright_if_idle_locked()

    return result
