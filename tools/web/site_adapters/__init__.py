"""Lightweight per-site adapters that extend browser_automation.

Adapters stay small on purpose. Each one exposes named landmarks for flows
that generic selector inspection keeps missing, so the model can aim for a
real target instead of guessing on hashed React classes.
"""

from tools.web.site_adapters import instagram


_ADAPTERS = {
    'instagram': instagram,
}


def get_adapter(site_name: str):
    return _ADAPTERS.get((site_name or '').lower())


def landmark_selectors(site_name: str) -> list[dict]:
    """Return the adapter's landmark candidates as inspect-style entries."""
    adapter = get_adapter(site_name)
    if adapter is None:
        return []
    return list(adapter.LANDMARKS)
