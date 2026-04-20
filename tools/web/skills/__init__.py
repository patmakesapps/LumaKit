"""Site-specific skill handlers for the browse tool.

A skill is a deterministic flow (open_dm, send_dm, check_inbox) that owns
its own observe -> act -> verify loop. Handlers live per-site; this module
just resolves (site, action) to the callable.
"""

from tools.web.skills import instagram


_SKILLS = {
    'instagram': {
        'send_dm': instagram.send_dm,
        'check_inbox': instagram.check_inbox,
        'open_dm': instagram.open_dm,
    },
}


def get_skill(site: str, action: str):
    site_skills = _SKILLS.get((site or '').lower(), {})
    return site_skills.get((action or '').lower())


def list_skills() -> dict:
    return {
        site: sorted(actions.keys())
        for site, actions in _SKILLS.items()
    }
