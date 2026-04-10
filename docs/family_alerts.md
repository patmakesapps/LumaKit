# Family & Group Alerts

LumaKit is designed to run in a household with multiple people talking to the same agent through Telegram. This doc explains how reminders, memories, and alerts are scoped between individuals and the whole group.

## Authorized users

Everyone who chats with Lumi must be listed in `TELEGRAM_ALLOWED_IDS` in `.env`:

```env
TELEGRAM_ALLOWED_IDS=8760436715,1234567890,9876543210
```

- **First ID = owner/admin.** Only the owner can add new users, trigger email tools, approve email drafts, and use other owner-gated features.
- **Other IDs = household members.** They can chat with Lumi, set their own reminders, and receive family-wide alerts, but they can't touch owner-only tools.
- New users can be added at runtime via the `/adduser` command (owner only). The owner picks from a list of unauthorized IDs that have recently tried to message the bot, and the new user is persisted to `.lumakit/telegram_users.json` — `.env` stays untouched.

## Reminder & memory scoping

Every reminder and memory has a **scope**:

| Scope | Who sees/receives it | When to use |
|---|---|---|
| `me` (personal) | Only the person who created it | Default. "Remind me to call the dentist at 3pm" |
| `everyone` (family) | Every authorized user in the household | "Remind the whole family it's trash day tomorrow" |

The agent decides scope based on how you phrase the request:

- **"remind me..."** → `me`
- **"remind us..."**, **"tell everyone..."**, **"family reminder..."**, **"remind the whole house..."** → `everyone`
- When in doubt, the agent defaults to `me` to avoid accidentally pinging the whole household.

## How personal reminders fire

When a personal reminder comes due, only the creator gets pinged:

```
🔔 Reminder: Call the dentist
```

This arrives as a Telegram message sent exclusively to the creator's chat ID. Nobody else in the house sees it.

## How family reminders fire

When a family reminder comes due, every authorized chat ID gets a message:

```
🔔 Family reminder: Trash day tomorrow, bins out tonight
```

The same text is sent individually to every user in `ALLOWED_IDS` so everyone gets their own Telegram notification.

## Who can edit family reminders

Only the **creator** can modify or delete a reminder they made, even if it's a family reminder. This prevents household members from quietly deleting each other's alerts. The owner is not granted special edit rights on other people's reminders — if your spouse creates a family reminder and you want it gone, they have to be the one to remove it.

## Memories work the same way

Saving a fact or preference follows the same scope rules:

- **"remember my favorite color is blue"** → personal. Only you see it on `recall`.
- **"remember that the wifi password is `hunter2`"** (said casually) → personal by default. The agent asks or picks `me`.
- **"remember that everyone in the family prefers tea over coffee"** → family scope. Anyone in the house can `recall` it.

When someone runs `recall`, they see their own personal memories **plus** all family memories, never other people's personal memories.

## Owner-only features

These are strictly limited to the first ID in `TELEGRAM_ALLOWED_IDS`:

- **Email tools** — send, reply, check inbox, read, plus the background email checker's draft approvals (`yes`/`no` replies)
- **`/adduser`** — authorize new household members
- **`/users`** — list authorized users
- **Heartbeat check-ins** — Lumi sends periodic "you still alive?" messages only to the owner

Non-owners who try to use owner-gated tools get a polite refusal. The `core.auth` module tracks the current active user per turn and the email tools check `is_owner_active()` at the tool boundary — the LLM can't talk its way around it.

## Heartbeat

Independent of reminders, Lumi runs a `Heartbeat` thread that pings the owner if the chat has been idle for a while. The check is scoped to the owner only — family members don't get heartbeat pings, since the goal is to give one specific person a gentle "I'm still here" nudge, not to spam the whole house.

Tuneable in `telegram_bridge.py`:

```python
heartbeat = Heartbeat(send=heartbeat_send, interval=900, cooldown=3600)
```

- `interval` — how often the thread wakes up to decide whether to send (default 15 min)
- `cooldown` — minimum gap between actual pings (default 1 hour)

## Files involved

- `telegram_bridge.py` — the notify callbacks that decide which chat ID(s) to send to
- `core/memory_store.py` — stores the `chat_id` column that implements scoping (`NULL` = family, set = personal)
- `tools/memory/memory_tools.py` — the `scope: me | everyone` parameter on `remember` and the creator-only edit gate
- `core/reminder_checker.py` — background thread that finds due reminders and hands them to the notify callback
- `core/heartbeat.py` — owner-only idle check-ins
- `core/auth.py` — owner gate used by email and any other owner-only tool
