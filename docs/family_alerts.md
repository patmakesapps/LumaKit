# Family & Group Alerts

This guide reflects the current multi-user model in the repo. LumaKit supports multiple authorized Telegram users, but reminders, memories, conversations, and owner-gated features are now coordinated through the shared service and per-user runtime state rather than the older single-bridge assumptions.

## Authorized users

Set authorized Telegram users in `.env`:

```env
TELEGRAM_ALLOWED_IDS=8760436715,1234567890,9876543210
```

Current behavior:

- The first ID is the owner/admin.
- Additional IDs are authorized household members.
- The owner can add or remove users at runtime with `/adduser` and `/removeuser`.
- `/users` lists the currently authorized set.

The Telegram surface reads this state from `core.telegram_state`, and owner checks are enforced through `core.auth`.

## What is shared and what is personal

Multi-user support is not just reminders anymore. Current behavior is:

- Conversation history is per user.
- Personality overrides are per user.
- Personal reminders and memories are per user.
- Family reminders and family memories are shared across the household.
- Owner-only tools stay owner-only regardless of reminder scope.

That matches the README claim that household members get their own conversation history, personality overrides, and reminders.

## Reminder and memory scopes

Memory tools currently expose two scopes:

| Scope | Stored as | Who sees or receives it | Typical phrasing |
|---|---|---|---|
| `me` | `chat_id=<requester>` | Only the requester | "remind me to call mom" |
| `everyone` | `chat_id=NULL` | Entire household | "remind us about trash day" |

Implementation detail:

- `tools/memory/memory_tools.py` maps `scope="everyone"` to `chat_id=None`
- `core/memory_store.py` treats `chat_id IS NULL` as family/shared scope
- A user only sees their own personal items plus family items

If the agent is unsure, it should default to `me`.

## Personal reminders

A personal reminder is delivered only to the user who created it.

Example notification:

```text
🔔 Reminder: Call the dentist
```

The service routes that reminder back to the matching `chat_id`, not to the rest of the household.

## Family reminders

A family reminder is stored with no `chat_id`, which makes it a broadcast reminder.

Example:

```text
🔔 Family reminder: Trash day tomorrow, bins out tonight
```

Current routing behavior is important here:

- The shared service labels these as `Family reminder`
- The router targets `both`
- Each registered surface decides how to broadcast
- On Telegram, the reminder is sent to every ID in `ALLOWED_IDS`

So family reminders are household-wide by design, not just visible in one user's chat.

## Memory recall behavior

When a user runs `recall`, they get:

- their own personal memories
- all family memories

They do not get:

- other users' personal memories

That scoping is enforced in [core/memory_store.py](/home/patrick/LumaKit/core/memory_store.py) via `(chat_id = active_user OR chat_id IS NULL)`.

## Who can edit or delete reminders

Current protection is creator-based:

- If a memory row has `created_by` and there is an active Telegram user, only the creator can update or delete it.
- This applies even to family reminders and family memories.
- Legacy rows without `created_by` bypass that restriction.
- CLI mode also bypasses per-user creator checks because there is no active Telegram user.

That logic lives in `tools/memory/memory_tools.py` via `_check_owner()`.

## Owner-only features

These are still restricted to the first ID in `TELEGRAM_ALLOWED_IDS`:

- Email tools and email-draft approval
- `/adduser`
- `/removeuser`
- `/users`
- Owner model controls like `/model`
- Heartbeat check-ins

Household members can still chat, set reminders, use their own personality override, and receive family reminders.

## Heartbeat behavior

Heartbeat is now owned by the shared service, not hardwired inside an older bridge script.

Current behavior:

- Only the owner gets heartbeat messages
- Heartbeat memory lookups are scoped to the owner so other users' memories do not bleed in
- Future reminders are skipped during heartbeat context building so the owner is not pre-nagged before `notify_at`

Defaults from [core/service.py](/home/patrick/LumaKit/core/service.py):

- `heartbeat_interval=900`
- `heartbeat_cooldown=3600`

The implementation is in [core/heartbeat.py](/home/patrick/LumaKit/core/heartbeat.py).

## Service architecture

The biggest drift from the older doc is architectural:

- `core.service.LumaKitService` owns reminders, tasks, heartbeat, and email
- Surfaces such as Telegram and web register themselves as delivery targets
- Reminder routing is centralized instead of being hardcoded into one surface entrypoint

That means this feature set is no longer "Telegram bridge logic with a few household exceptions"; it is shared app behavior with Telegram as one delivery surface.

## Files involved

- [core/service.py](/home/patrick/LumaKit/core/service.py) — shared service, router, family-reminder broadcast behavior
- [surfaces/telegram.py](/home/patrick/LumaKit/surfaces/telegram.py) — Telegram delivery for personal and family reminders
- [tools/memory/memory_tools.py](/home/patrick/LumaKit/tools/memory/memory_tools.py) — scope parsing, active user binding, creator-only edits
- [core/memory_store.py](/home/patrick/LumaKit/core/memory_store.py) — SQLite scoping model using `chat_id` and `created_by`
- [core/heartbeat.py](/home/patrick/LumaKit/core/heartbeat.py) — owner-only heartbeat with owner-scoped memory lookup
