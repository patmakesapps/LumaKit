# Windows Quick Start

This is the fastest path to a working LumaKit install on Windows.

## 1. Install Ollama

Install Ollama first and make sure it is running locally.

In PowerShell:

```powershell
ollama list
```

If that works, LumaKit can talk to your local Ollama endpoint.

## 2. Pick a model

Choose the model you want LumaKit to use and make sure your Ollama setup can serve it.

If you want the strongest first-run impression, use a strong tool-capable model. Small models can chat, but they are more brittle with multi-step tool loops.

## 3. Clone the repo

```powershell
git clone https://github.com/patmakesapps/LumaKit.git
cd LumaKit
```

## 4. Install dependencies

```powershell
py -m pip install -r requirements.txt
playwright install chromium
py -m pip install -e .
```

## 5. Create `.env`

```powershell
copy .env.example .env
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

## 6. Launch LumaKit

```powershell
lumakit open
```

That should:

- start the backend if it is not already running
- reuse it if it is already running
- open the web UI in your browser

## 7. Install the Windows shortcuts

```powershell
lumakit shortcut install
```

When native shortcut creation succeeds, that writes:

- a Desktop shortcut
- a Start Menu shortcut

Both launch `lumakit open`.

## Useful commands

```powershell
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
- [Launcher Commands](launcher.md)
