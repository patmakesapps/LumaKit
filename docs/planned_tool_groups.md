# Planned: per-chat tool selection ("Tools" popup)

**Status:** planned, not started. Nothing in this document is implemented.
**Origin:** live testing on 2026-09-15. A trivial "fix the failing test" task
carried all 110 tool schemas on every model round (about 15k prompt tokens
before the model said a word), and a small local model (dolphin3-tools)
reached for `create_task` to create a folder because it had too many
plausible tools to choose from.

## The idea

A **Tools** button in the chat opens a popup listing the tool groups. The
user picks, per chat, which groups are sent to the model. A chat used with
a small local model can drop everything it doesn't need; a chat driving
LumaBot can keep the robot tools and drop the rest.

Defaults to everything on, so existing chats behave exactly as they do now.

## Why it is worth doing

| | Today | Coding-only selection |
|---|---|---|
| Tools sent per round | 110 | about 60 |
| Schema tokens per round (gemma4, measured) | about 15k | about 9k |
| Wrong-tool grabs on small models | common | fewer choices, fewer mistakes |

Fewer schemas means faster rounds on local models, lower cost on hosted
ones, and better tool choice, which matters most for exactly the models
that need it.

## What already exists (no new machinery needed)

- **Tools are grouped by folder** and the registry can already list by
  group: `ToolRegistry.list(groups=...)` in `tool_registry.py`.
  Group sizes: repo 27, runtime 20, struqt 15, lumabot 11, code_intel 9,
  comms 8, lumalok 8, web 5, memory 4.
- **The agent already filters tools per request.** Telegram roles drop
  denied tools in `Agent._filter_role_denied_tools` (agent.py), right after
  the schema cache. A per-chat group filter plugs in at the same point.
- **Per-chat settings already exist.** The LumaBot mode lives in
  `chat_runtime_modes` (core/chat_store.py). Tool groups become one more
  column there.
- **The composer already has a tools button** with an on/off menu
  (`#tools-btn`, `#tools-menu` in web/index.html). The popup is that menu
  grown up; the existing "tools off" state becomes "no groups selected".

## Design decisions

1. **Per group, not per tool.** Nine group toggles plus presets. 110
   checkboxes is unusable, and single-tool gaps break workflows in ways
   users cannot diagnose. A per-tool "advanced" expander can come later if
   anyone asks for it.
2. **Presets:** *Everything* (default), *Coding* (repo, runtime,
   code_intel, memory), *Minimal* (runtime, memory). Presets are just
   group sets; the user can adjust after picking one.
3. **Load-bearing groups stay on.** `runtime` (workspace, tasks, run
   commands) and `memory` are shown but locked on, or at least flagged as
   recommended, so nobody strands a chat.
4. **Scope is the chat.** The selection is stored per chat and travels
   with it across reloads and surfaces. A global default can be added
   later in Settings if it proves useful.
5. **Tasks inherit.** A task created from a chat copies that chat's groups
   into its constraints (`_tool_groups`) and the runner builds its tool
   list from them. Otherwise a task created from a trimmed chat would carry
   all 110 tools again.
6. **The prompt follows the tools.** The system prompt's tool-name list is
   already derived from the registry; it must be built from the filtered
   set. The group-specific rules in the prompt (Struqt, Instagram/browser,
   email, LumaBot) drop out when their group is off, so a small model is
   not told about tools it cannot call.

## Implementation sketch (about a day)

Five contained changes, in this order so each step is testable alone:

1. **Store:** `chat_store.set_chat_tool_groups(chat_id, groups)` /
   `get_chat_tool_groups(chat_id)`; `None` means everything.
2. **Agent:** apply the chat's groups in the existing filter hook; build
   the prompt's tool-name list and the group-specific rules from the
   filtered set; invalidate the schema cache when the selection changes.
   `apply_user_runtime` / `apply_chat_runtime` already re-apply per-chat
   state on every turn, so this rides the same path as the LumaBot mode.
3. **Tasks:** copy the groups into constraints at creation (web API and
   the `create_task` tool); `TaskRunner._build_tool_list` honours them.
4. **API + socket:** `GET/POST /api/chats/{id}/tools`, plus a
   `chat_tools` socket message so the composer badge updates live.
5. **UI:** grow the tools menu into the popup: presets on top, group rows
   with counts and one-line hints, locked groups marked. Show a small
   "N tools" badge on the button so a trimmed chat is visible at a glance.

Verification: a trace (`lumakit trace <id>`) of the demo task before and
after selecting *Coding* should show the per-round prompt tokens drop and
the same outcome.

## Risks and how they are handled

- **Model asks for a tool that is off.** The registry returns the normal
  unknown-tool error; the prompt no longer lists it. Acceptable, and the
  popup makes turning it back on a two-click fix.
- **Prompt caching (hosted providers).** Changing the tool set invalidates
  the cached prefix once; it is stable per chat afterwards.
- **Telegram roles.** Role denials still apply on top of the chat
  selection; the two filters compose (role denial wins).
- **Users stranding themselves.** Locked groups plus the default of
  everything on.

## Out of scope for the first version

- Per-tool toggles.
- Automatic selection from the goal text (for example, dropping email
  tools when the goal is a code change). Worth revisiting once traces show
  which groups real tasks actually use.
- A global default in Settings.
