# LumaKit

![LumaKit Logo](photos/lumakit_cat_logo.png)

LumaKit is a local AI agent that talks to an Ollama model and gives it access to repo, runtime, web, and communication tools. It runs as a background service and can be controlled from Telegram, the CLI, or autonomously via the task runner.

## Features

- **Tool-calling agent** — loads tools automatically from `tools/` and lets the model call them in multi-round loops
- **Autonomous task runner** — give Lumi a goal and a deadline; it plans, executes steps, self-evaluates, and reports back. Survives restarts — all state is persisted in SQLite
- **CLI interface** — interactive chat with slash commands, clipboard image pasting, chat persistence, and storage management
- **Telegram bridge** — chat with LumaKit from your phone; supports multiple authorized users, photo/vision analysis, optional local voice-note transcription (whisper.cpp), optional `edge-tts` voice replies (sent as voice memos), and admin controls
- **Autonomous email** — give the agent its own Gmail account; it polls every 60s, drafts replies for the owner, and requires one-tap approval before sending (owner-only, with codebase-leak filter, rate limiting, and URL stripping). See [docs/gmail_setup.md](docs/gmail_setup.md)
- **Identity file** — `lumi/identity.txt` stores Lumi's own accounts and credentials; surfaced in the system prompt so Lumi checks it before signing up for new services and appends new accounts after creating them
- **Family & personal reminders** — per-user reminders plus household-wide broadcasts. See [docs/family_alerts.md](docs/family_alerts.md)
- **Screenshot tool** — the agent can grab the current screen and push it to the owner on Telegram
- **Heartbeat** — background check-ins from Lumi when the owner has been quiet
- **Code intelligence** — built-in code index using tree-sitter for symbol lookup, definition finding, usage search, and call graphs
- **Memory & reminders** — persistent SQLite memory store and a background reminder system
- **Context management** — automatic conversation summarization to keep context lean
- **Storage budgeting** — tracks local data usage with configurable budgets and cleanup prompts
- **Fallback model support** — automatically falls back to a secondary Ollama model if the primary is unavailable
- **Image/vision support** — send images via CLI (`/p`, `/image`) or Telegram photos for vision-capable models

## Model Note

Tool calling quality depends heavily on the model you run through Ollama. Smaller models may answer basic prompts fine, but they can be less reliable when choosing tools, formatting arguments, or handling multi-step loops. If a tool seems inconsistent, test with a stronger model before assuming the tool is broken.

## Requirements

- Python 3.10+
- Ollama running locally at `http://localhost:11434`
- An Ollama model pulled locally
- `ffmpeg` recommended if you plan to work with Telegram audio frequently
- For Telegram speech: local `whisper.cpp` build plus `edge-tts` installed in the same Python environment you use to run the bridge

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

For Telegram speech, also build `whisper.cpp` locally and make sure its `base.en` model is downloaded.

## Configuration

Copy `.env.example` to `.env` and set the values you want to use.

| Variable | Purpose |
|---|---|
| `OLLAMA_MODEL` | Primary model for chat requests |
| `OLLAMA_FALLBACK_MODEL` | Fallback model if primary is unavailable |
| `OLLAMA_LOCAL_MODEL` | Optional local model the Telegram owner can switch to with `/model local on` |
| `SERPAPI_KEY` | Optional — enables premium web search |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (for Telegram bridge) |
| `TELEGRAM_ALLOWED_IDS` | Comma-separated Telegram chat IDs (first = owner/admin) |
| `LUMIKIT_WHISPER_DIR` | Optional — path to the local `whisper.cpp` checkout |
| `LUMIKIT_WHISPER_BIN` | Optional — path to the local `whisper-cli` binary |
| `LUMIKIT_WHISPER_MODEL` | Optional — path to the local Whisper model file |
| `LUMIKIT_TTS_VOICE` | Optional — default Edge voice name, default `en-US-AvaNeural` |
| `LUMIKIT_TTS_FORMAT` | Optional — audio format for Telegram replies, default `mp3` |
| `LUMI_EMAIL_ADDRESS` | Lumi's own Gmail address (for the autonomous email loop) |
| `LUMI_EMAIL_PASSWORD` | Gmail app password (see [docs/gmail_setup.md](docs/gmail_setup.md)) |
| `LUMI_EMAIL_SIGNATURE` | Signature appended to every outbound email |
| `LUMI_EMAIL_MAX_PER_HOUR` | Rate limit on outbound email sends (default 10) |

## Usage

### CLI

```bash
python main.py
```

Verbose mode:

```bash
python main.py --verbose
```

CLI commands:

| Command | Action |
|---|---|
| `/p [prompt]` | Paste clipboard image and optionally describe it |
| `/image <path> [prompt]` | Send an image file to the model |
| `/help` | Show all available commands |
| `exit` / `quit` | End the session |

### Telegram Bridge

```bash
python telegram_bridge.py
```

Or run as a systemd service (see [docs/autostart.md](docs/autostart.md)).

Telegram commands:

| Command | Action |
|---|---|
| `/help` | Show available commands |
| `/chats` | List and resume saved conversations |
| `/new` | Start a fresh conversation |
| `/status` | Show model, storage, and index info |
| `/tasks` | List autonomous background tasks |
| `/task <id>` | Show details and history for a specific task |
| `/voice ...` | Enable audio replies, list voices, or set your preferred Edge voice |
| `/adduser` | (Owner only) Authorize a new user |
| `/removeuser` | (Owner only) Remove an authorized user |
| `/model` | (Owner only) Open a Telegram menu to change the owner's primary, fallback, or local-model mode |
| `/personality` | View or change your own Telegram personality override |
| `/users` | (Owner only) List authorized users |

You can also send photos directly — LumaKit will analyze them if the model supports vision.
If local speech is configured, you can send Telegram voice notes or audio files and LumaKit will transcribe them before replying.

### Autonomous Tasks

Tell Lumi a goal with a deadline and it will handle the rest:

> *"Research the best dividend ETFs available right now — compare yield, expense ratio, and 1-year return. Report back by tonight."*

> *"Monitor the Bitcoin price every 15 minutes for the next hour and send me updates."*

Lumi will:
1. Generate a step-by-step plan and confirm it with you
2. Execute each step using its full tool suite (web search, browser, code execution, etc.)
3. Self-evaluate after each step and retry or escalate if stuck
4. Ping you on Telegram when blocked and wait for your input
5. Send a final report when done or when the deadline arrives

Use `/tasks` to check status or `/task <id>` for full history. Tasks survive service restarts.

## Project Structure

```
main.py                 CLI entry point
agent.py                Core agent loop (tool dispatch, diff preview, confirmation)
ollama_client.py        Ollama HTTP client with fallback and timeout support
telegram_bridge.py      Telegram bridge entry point — poll loop and background services
tool_registry.py        Auto-discovers and registers tools from tools/

core/
  auth.py               Owner gating (used by email tools)
  chat_store.py         SQLite-backed conversation persistence
  cli.py                Terminal UI helpers (spinner, colors, diffs)
  commands.py           CLI slash-command handler
  diffs.py              Unified diff generation
  email_checker.py      Background IMAP poller + LLM triage + one-shot draft approval
  email_filter.py       URL stripper, codebase-leak scanner, rate limiter, audit log
  heartbeat.py          Periodic owner check-ins when chat has been idle
  memory_store.py       SQLite-backed memory/reminder storage
  menu.py               Interactive selection menu
  paths.py              Repo root detection and path resolution
  reminder_checker.py   Background reminder polling thread
  storage.py            Storage budget tracking
  summarizer.py         Conversation summarization logic
  task_runner.py        Autonomous task execution engine — plan, execute, evaluate, report
  task_store.py         SQLite-backed task persistence (memory/tasks.db)
  telegram_api.py       Raw Telegram Bot API helpers (send, download, poll)
  telegram_commands.py  Telegram slash-command handlers and session/runtime management
  telegram_io.py        Telegram I/O primitives — send_message, TTS dispatch, polling, confirm
  telegram_speech.py    Local STT (whisper.cpp) and TTS (edge-tts) helpers
  telegram_state.py     Global bridge state — sessions, user configs, offset tracker

tools/
  code_intel/           Code index (tree-sitter) — symbol table, parsers, cache
  comms/                Communication tools (telegram, email, screenshot, reactions)
  memory/               Memory and reminder tools (save, recall, remind)
  repo/                 File and git operations (read, write, edit, delete, search, diff, git)
  runtime/              Shell, Python, system tools (restart_service, storage, clipboard*)
  web/                  HTTP fetch and web search

lumi/                   Lumi's private data — gitignored
  identity.txt          Lumi's accounts, credentials, and site logins
```

*Clipboard tools require a display and are not available in headless/server mode.*

## Adding Tools

Tools are auto-registered from `tools/**/*.py`. To add a new tool, follow the guidance in [CONTRIBUTING.md](CONTRIBUTING.md).

## Git Workflow

- Branch from `main` for each discrete task
- Prefer short-lived branches such as `feat/add-web-tool` or `fix/tool-registry-validation`
- Keep `main` stable and merge completed work back into `main`
- If your branch gets behind, merge `main` into your branch instead of rebasing
