# LumaKit

![LumaKit Logo](photos/lumakit_cat_logo.png)

LumaKit is a local CLI agent that talks to an Ollama model and gives it access to repo, runtime, web, and communication tools. It also supports Telegram as a chat interface with multi-user sessions.

## Features

- **Tool-calling agent** — loads tools automatically from `tools/` and lets the model call them in multi-round loops
- **CLI interface** — interactive chat with slash commands, clipboard image pasting, chat persistence, and storage management
- **Telegram bridge** — chat with LumaKit from your phone; supports multiple authorized users, photo/vision analysis, and admin controls
- **Code intelligence** — built-in code index using tree-sitter for symbol lookup, definition finding, usage search, and call graphs
- **Memory & reminders** — persistent memory store and a background reminder system
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

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and set the values you want to use.

| Variable | Purpose |
|---|---|
| `OLLAMA_MODEL` | Primary model for chat requests |
| `OLLAMA_FALLBACK_MODEL` | Fallback model if primary is unavailable |
| `SERPAPI_KEY` | Optional — enables premium web search |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (for Telegram bridge) |
| `TELEGRAM_ALLOWED_IDS` | Comma-separated Telegram chat IDs (first = owner/admin) |

Recommended model: `glm-5:cloud`

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

Telegram commands:

| Command | Action |
|---|---|
| `/help` | Show available commands |
| `/chats` | List and resume saved conversations |
| `/new` | Start a fresh conversation |
| `/status` | Show model, storage, and index info |
| `/adduser` | (Owner only) Authorize a new user |
| `/users` | (Owner only) List authorized users |

You can also send photos directly — LumaKit will analyze them if the model supports vision.

## Project Structure

```
main.py                 CLI entry point
agent.py                Core agent loop (tool dispatch, diff preview, confirmation)
ollama_client.py        Ollama HTTP client with fallback and timeout support
telegram_bridge.py      Telegram bot bridge with multi-user sessions
tool_registry.py        Auto-discovers and registers tools from tools/

core/                   Internal modules
  chat_store.py         SQLite-backed conversation persistence
  cli.py                Terminal UI helpers (spinner, colors, diffs)
  commands.py           CLI slash-command handler
  diffs.py              Unified diff generation
  memory_store.py       SQLite-backed memory/reminder storage
  menu.py               Interactive selection menu
  paths.py              Repo root detection and path resolution
  reminder_checker.py   Background reminder polling thread
  storage.py            Storage budget tracking
  summarizer.py         Conversation summarization logic

tools/
  code_intel/           Code index (tree-sitter) — symbol table, parsers, cache
  comms/                Communication tools (send_telegram)
  memory/               Memory and reminder tools (save, recall, remind)
  repo/                 File and git operations (read, write, edit, delete, search, diff, git)
  runtime/              Shell, Python, clipboard, system info, storage tools
  web/                  HTTP fetch and web search
```

## Adding Tools

Tools are auto-registered from `tools/**/*.py`. To add a new tool, follow the guidance in [CONTRIBUTING.md](CONTRIBUTING.md).

## Git Workflow

- Branch from `main` for each discrete task
- Prefer short-lived branches such as `feat/add-web-tool` or `fix/tool-registry-validation`
- Keep `main` stable and merge completed work back into `main`
- If your branch gets behind, merge `main` into your branch instead of rebasing
