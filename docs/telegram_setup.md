# Connecting Telegram to Your Agent

This guide walks you through giving Lumi a Telegram presence so you can chat with her from your phone, send photos for vision analysis, record voice notes, kick off autonomous tasks, and manage the whole household's access — all from anywhere.

## What you'll get

Once set up, you'll be able to:

1. **Message Lumi from your phone** like any other Telegram contact
2. **Send photos** and have Lumi analyze them (if your model supports vision)
3. **Record voice notes** — Lumi transcribes locally via `whisper.cpp` and replies (optional)
4. **Hear her talk back** as Edge-TTS voice memos (optional)
5. **Authorize other household members** at runtime with `/adduser` — each person gets their own conversation, personality, and reminders
6. **Kick off autonomous tasks** with a goal and deadline, and get a Telegram report when done
7. **Confirm destructive actions** via one-tap yes/no on Telegram before the agent commits

The first chat ID in `TELEGRAM_ALLOWED_IDS` is the **owner** — the only user who can use admin commands (`/adduser`, `/removeuser`, `/model`, `/users`), trigger email sends, and approve email drafts.

## Step 1 — Create a bot with @BotFather

Telegram bots are free and take about 60 seconds to provision.

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Give your bot a display name (e.g. `Lumi`) — what shows up in chat
4. Give it a username ending in `bot` (e.g. `your_lumi_bot`) — this is unique and public
5. BotFather replies with a token that looks like `1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`

Copy the token. **Treat it like a password** — anyone with it can impersonate your bot.

Optional BotFather tweaks (all via the chat):
- `/setdescription` — short tagline shown on the bot's profile
- `/setuserpic` — give your bot a face
- `/setcommands` — populate the `/` command menu that Telegram shows in the input box. Paste this list:
  ```
  help - Show available commands
  new - Start a fresh conversation
  chats - List and resume saved conversations
  stop - Interrupt Lumi mid-task
  status - Show model, storage, and index info
  tasks - List autonomous background tasks
  voice - Toggle voice replies and pick a voice
  personality - View or change your personality override
  ```

## Step 2 — Get your Telegram chat ID

You need your numeric Telegram chat ID so LumaKit knows who you are. The easiest way:

1. Open Telegram and start a chat with [@userinfobot](https://t.me/userinfobot)
2. Send any message
3. It replies with your ID — a number like `123456789`

Repeat for every other household member you want to authorize up front.

## Step 3 — Add credentials to `.env`

Open `.env` and set:

```env
TELEGRAM_BOT_TOKEN="1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
TELEGRAM_ALLOWED_IDS="123456789,987654321"
```

`TELEGRAM_ALLOWED_IDS` is comma-separated. **The first ID is the owner/admin.** You can leave it as just your own ID and add others later via `/adduser` from the chat.

**Never commit `.env`.** The `.gitignore` already excludes it, but double-check before pushing.

## Step 4 — Start the bridge

```bash
python telegram_bridge.py
```

You should see:

```
Telegram bridge running. 1 authorized user(s).
```

If you also configured email, you'll see the email checker seed line too.

## Step 5 — Say hi

Open Telegram, find your bot (search by the `@username` you picked with BotFather), and send `Hi`. Lumi should reply within a couple seconds using your configured `OLLAMA_MODEL`.

Try a few things:
- Send a photo — if your model supports vision, ask Lumi what's in it
- Send `/help` — see every command
- Send `/tasks` — see the autonomous task list (empty at first)
- Send `/status` — model, storage, memory info

## Adding household members

You have two options:

**Option A — Up front in `.env`:** Add their chat IDs to the comma-separated `TELEGRAM_ALLOWED_IDS` list and restart the bridge.

**Option B — At runtime:**

1. Have the new user message your bot from their Telegram. The bridge will refuse them but **queue their ID** as a pending user.
2. You (the owner) send `/adduser` — LumaKit shows the list of pending requests and lets you approve them one by one.
3. Approved users are persisted to `.lumakit/users.json` and stay authorized across restarts.

To remove a user, send `/removeuser` as the owner. Owner cannot remove themselves.

Each user gets their own:
- Conversation history (persisted independently)
- Personality override (`/personality`)
- Reminders and memory scope
- Voice preference (`/voice`)

## Voice (optional)

LumaKit supports two-way voice via local `whisper.cpp` for STT and `edge-tts` for TTS. Both run locally — no cloud round-trip, no API keys.

### Setup

1. **whisper.cpp** — clone and build:
   ```bash
   cd .vendor  # or wherever you want it
   git clone https://github.com/ggerganov/whisper.cpp
   cd whisper.cpp
   make
   bash ./models/download-ggml-model.sh base.en
   ```
   The `whisper-cli` binary lands at `.vendor/whisper.cpp/build/bin/whisper-cli` and the model at `.vendor/whisper.cpp/models/ggml-base.en.bin`.

2. **edge-tts** — install into the same Python env as the bridge:
   ```bash
   pip install edge-tts
   ```

3. **ffmpeg** — required for Telegram voice-memo conversion:
   ```bash
   sudo apt install ffmpeg   # or brew install ffmpeg on macOS
   ```

4. **`.env` paths:**
   ```env
   LUMIKIT_WHISPER_DIR=".vendor/whisper.cpp"
   LUMIKIT_WHISPER_BIN=".vendor/whisper.cpp/build/bin/whisper-cli"
   LUMIKIT_WHISPER_MODEL=".vendor/whisper.cpp/models/ggml-base.en.bin"
   LUMIKIT_TTS_VOICE="en-US-AvaNeural"
   LUMIKIT_TTS_FORMAT="mp3"
   ```

### Usage

- Send a voice note or any audio file → Lumi transcribes it and replies in text by default
- `/voice on` → replies come back as voice memos too
- `/voice off` → text only
- `/voice list` → list available Edge voices
- `/voice <voice-name>` → pick a specific Edge voice (e.g. `en-US-JennyNeural`)

## Run as a service

Once the bridge is working, you probably want it always-on. See [autostart.md](autostart.md) for the systemd setup.

## Troubleshooting

**"TELEGRAM_BOT_TOKEN not set"**
Your `.env` isn't loaded or the token is missing. Check that `.env` lives in the project root and that you restarted the bridge after editing.

**"Telegram API error: 401 Unauthorized"**
The bot token is wrong. Re-copy it from @BotFather (`/mybots` → pick your bot → `API Token`).

**You message the bot but get no reply — not even a rejection**
Verify the bridge is actually running: `ps aux | grep telegram_bridge`. If it's running, tail its output — you should see `[telegram] update from chat_id=…` lines when messages arrive. If nothing shows, Telegram isn't reaching your bridge — check your network and that the bot isn't being used by another process holding the long-poll lock.

**"User not authorized"**
Your chat ID isn't in `TELEGRAM_ALLOWED_IDS` and hasn't been approved via `/adduser`. Either add it to `.env` and restart, or have the owner send `/adduser` from their own chat.

**Voice notes arrive but transcription is silent**
Check the bridge output for whisper errors. Most common causes: `whisper-cli` binary path wrong, model file missing, or `ffmpeg` not installed (Telegram voice memos are `.ogg` and need conversion before whisper.cpp can read them).

**Voice replies don't send**
Edge-TTS needs an outbound connection to Microsoft's endpoint. Verify `edge-tts --list-voices` runs from the same Python env as the bridge.

**Bridge crashes with `MultipleBotInstances` or the bot replies twice**
Two copies of `telegram_bridge.py` are running. Telegram only lets one process long-poll at a time. Kill the stray one.

**Commands don't show up in the `/` menu on Telegram**
Re-run `/setcommands` on @BotFather and paste the list. It can take a minute to propagate to the client.

## Commands reference

| Command | Scope | Action |
|---|---|---|
| `/help` | all | Show available commands |
| `/new` | all | Start a fresh conversation |
| `/chats` | all | List and resume saved conversations |
| `/stop` | all | Interrupt Lumi mid-task |
| `/status` | all | Show model, storage, and index info |
| `/tasks` | all | List autonomous background tasks |
| `/task <id>` | all | Show details for a specific task |
| `/voice ...` | all | Enable/disable voice replies, list voices, pick a voice |
| `/personality` | all | View or change your personality override |
| `/tools` | all | Toggle tool-call visibility in replies |
| `/adduser` | owner | Approve pending user requests |
| `/removeuser` | owner | Remove an authorized user |
| `/users` | owner | List authorized users |
| `/model` | owner | Change the owner's primary, fallback, or local model |

## Files involved

- `telegram_bridge.py` — main bridge entry point and poll loop
- `core/telegram_api.py` — raw Telegram Bot API helpers
- `core/telegram_io.py` — `send_message`, voice-reply dispatch, polling, owner confirm
- `core/telegram_commands.py` — slash-command handlers
- `core/telegram_state.py` — token, allowed IDs, pending users, offset tracker
- `core/telegram_speech.py` — whisper.cpp + edge-tts helpers
- `core/telegram_user_config.py` / `core/telegram_owner_config.py` — per-user and owner runtime preferences
- `.lumakit/users.json` — persisted list of runtime-approved users
