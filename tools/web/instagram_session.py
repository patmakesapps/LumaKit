"""
Instagram session bootstrapper.

Thin entrypoint for Instagram workflows. Two jobs:
1. Report the status of the persistent 'instagram' browser auth profile.
2. Load the self-maintained Instagram notes file (selectors, quirks, shortcuts)
   and inject it into Lumi's working context.

After calling this, continue with browser_automation using auth_profile='instagram'.
Lumi maintains the notes herself by calling this tool again with add_note=<learning>.
"""

from datetime import datetime
from pathlib import Path

from core.paths import get_repo_root


_PROFILE_NAME = 'instagram'


def _profile_path() -> Path:
    return Path.home() / '.lumakit' / 'browser_profiles' / f'{_PROFILE_NAME}.json'


def _notes_path() -> Path:
    return get_repo_root() / 'instagram' / 'notes.md'


_SEED_NOTES = """# Instagram session notes

Self-maintained reference for agent_lumi's Instagram workflows. Append new
learnings here the moment you discover them — selectors that worked, UI
quirks, shortcuts — anything that would save time next session.

## Known quirks

- Instagram's primary buttons (Log In, Post, Like, Follow) often render as
  `<div role="button">` instead of `<button>`. Prefer `div[role='button']`
  selectors or `:has-text('...')` matchers over `button[...]`. Always call
  inspect_forms before guessing.

## Working selectors

## Navigation shortcuts
"""


def _instagram_session(inputs):
    add_note = (inputs.get('add_note') or '').strip()

    notes_path = _notes_path()
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    if not notes_path.exists():
        notes_path.write_text(_SEED_NOTES, encoding='utf-8')

    if add_note:
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        with notes_path.open('a', encoding='utf-8') as fh:
            fh.write(f'\n- ({stamp}) {add_note}\n')

    profile = _profile_path()
    if profile.exists():
        auth = {
            'logged_in': True,
            'profile_path': str(profile),
            'last_saved': datetime.fromtimestamp(
                profile.stat().st_mtime
            ).isoformat(timespec='seconds'),
        }
    else:
        auth = {
            'logged_in': False,
            'profile_path': str(profile),
            'next_step': (
                'No saved Instagram login found. Use browser_automation with '
                "auth_profile='instagram' and log in manually once. The "
                'session will be saved automatically for future calls.'
            ),
        }

    return {
        'auth': auth,
        'notes_path': str(notes_path),
        'notes': notes_path.read_text(encoding='utf-8'),
        'next_step': (
            "Continue with browser_automation, passing auth_profile='instagram'. "
            'Read the notes above before navigating. Call instagram_session again '
            "with add_note='<learning>' whenever you discover something new "
            '(a working selector, a quirk, a shortcut) so your future self '
            'skips the rediscovery.'
        ),
    }


def get_instagram_session_tool():
    return {
        'name': 'instagram_session',
        'description': (
            'Bootstrap an Instagram browsing session. Call this BEFORE using '
            'browser_automation for anything Instagram-related. Returns: '
            "(1) status of the persistent 'instagram' auth profile (are you "
            'already logged in?), (2) the self-maintained notes file with '
            'known selectors, quirks, and shortcuts from past sessions, and '
            '(3) instructions for the next browser_automation call.\n\n'
            'Use add_note to append a learning to the notes file — do this '
            'any time you discover a working selector, a UI quirk, or a '
            'navigation shortcut. The notes persist across sessions and are '
            're-injected at the start of every Instagram task, so a few '
            'seconds writing a good note saves minutes every future session.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'add_note': {
                    'type': 'string',
                    'description': (
                        'Optional. Append this note to the Instagram notes '
                        'file (dated automatically). Use for newly discovered '
                        'selectors, quirks, or shortcuts.'
                    ),
                },
            },
        },
        'execute': _instagram_session,
    }
