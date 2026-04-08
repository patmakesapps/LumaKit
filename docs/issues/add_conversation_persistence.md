# Title

Add optional conversation persistence between sessions

# Suggested labels

`enhancement`, `core`, `good first issue`

# Body

## Problem

The agent trims history but doesn't save it between sessions. If you quit and restart, all context is lost. This means users lose valuable conversation state and must re-explain context every time.

## Proposed solution

- Add an optional session persistence layer that saves conversation history to disk.
- Store sessions as JSON files in a configurable directory (e.g. `~/.lumakit/sessions/`).
- On startup, offer to resume the most recent session or start fresh.
- Respect the existing history trimming logic — only persist the trimmed version.

## Implementation notes

- Add a `SessionStore` class in `core/session.py` that handles save/load.
- Use a simple file-based approach (one JSON file per session).
- Add a `--resume` CLI flag to restore the last session.
- Add a `--no-persist` flag to disable saving.
- Keep it optional so the default behavior is unchanged.

## Acceptance criteria

- Conversation history survives a quit and restart when persistence is enabled.
- Sessions are stored in a human-readable format.
- The feature is off by default or opt-in to avoid surprises.
- Old sessions can be listed and deleted.

## Out of scope

- Cloud-based session sync
- Multi-user session sharing
- Encryption of stored sessions
