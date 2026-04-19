# Linux Quick Start

This is the fastest path to a working LumaKit install on Linux.

## 1. Install Ollama

Install Ollama first and make sure the daemon is running locally.

```bash
ollama list
```

If that command works, you are ready for LumaKit.

## 2. Pick a model

Choose the model you want LumaKit to use and make sure it is available through your local Ollama setup.

LumaKit is much better with a strong tool-capable model than with a tiny local model. If the agent feels weak, that is usually the model, not the launcher.

## 3. Clone the repo

```bash
git clone https://github.com/patmakesapps/LumaKit.git
cd LumaKit
```

## 4. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
pip install -e .
```

## 5. Create `.env`

```bash
cp .env.example .env
```

Set at least:

```env
OLLAMA_MODEL="your-model-here"
```

Optional but common:

```env
OLLAMA_FALLBACK_MODEL="your-fallback-model"
LUMAKIT_WEB_PORT="7865"
```

If you prefer, you can leave `OLLAMA_MODEL` blank and choose your primary model from the web UI on first launch. The app will block chat until a model is selected.

## 6. Launch LumaKit

```bash
lumakit open
```

That should:

- start the backend if it is not already running
- reuse it if it is already running
- open the web UI in your browser

If no model is configured in `.env` or app settings yet, LumaKit opens into a first-run setup state and asks you to choose one in Settings before chatting.

## 7. Install the Linux launcher

If you want LumaKit to show up like a normal app:

```bash
lumakit shortcut install
```

On Linux, this installs the app-menu launcher in:

```text
~/.local/share/applications/lumakit.desktop
```

Use the app menu to launch it. That is the intended Linux UX.

## Useful commands

```bash
lumakit status
lumakit stop
lumakit serve
```

- `lumakit status` shows whether the backend is already running
- `lumakit stop` stops the backend
- `lumakit serve` runs in the foreground for debugging

## Optional next steps

- [Telegram Setup](telegram_setup.md)
- [Gmail Setup](gmail_setup.md)
- [Autostart / systemd](autostart.md)
- [Launcher Commands](launcher.md)
