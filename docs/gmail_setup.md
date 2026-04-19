# Connecting Gmail to Your Agent

This guide matches the current repo layout and runtime. Gmail support is still owner-only, but it now lives behind the shared `LumaKitService` background workers rather than the older standalone `telegram_bridge.py` flow.

## What you get

Once configured, LumaKit can:

1. Poll Lumi's inbox every 60 seconds over IMAP
2. Strip URLs out of inbound mail before the model sees the body
3. Ask the model for a short summary and, when appropriate, a draft reply
4. Notify the owner on the active owner surface
5. Wait for explicit owner approval before anything sends
6. Run the outbound safety pipeline: leak scan, rate limit, signature, audit log, SMTP send

Email tools are owner-only. The owner is the first Telegram ID in `TELEGRAM_ALLOWED_IDS`, and the tool boundary enforces that in `core.auth`.

## Step 1 — Create a Gmail account for Lumi

Use a dedicated account instead of your personal inbox, for example `lumifromyourname@gmail.com`.

## Step 2 — Enable 2FA

Gmail app passwords require 2-Step Verification.

1. Sign in to the Gmail account.
2. Open `https://myaccount.google.com/security`.
3. Turn on 2-Step Verification.

## Step 3 — Generate an App Password

1. Open `https://myaccount.google.com/apppasswords`.
2. Create an app password for `Lumi` or any label you want.
3. Copy the generated 16-character password.

LumaKit strips spaces automatically when reading `LUMI_EMAIL_PASSWORD`.

## Step 4 — Add the env vars

Set these in your repo-root `.env` or in `~/.lumakit/config.env` if you want user-local overrides:

```env
LUMI_EMAIL_ADDRESS="lumifromyourname@gmail.com"
LUMI_EMAIL_PASSWORD="sqab elap uiko mrdz"
LUMI_EMAIL_SMTP_HOST="smtp.gmail.com"
LUMI_EMAIL_SMTP_PORT="465"
LUMI_EMAIL_IMAP_HOST="imap.gmail.com"
LUMI_EMAIL_IMAP_PORT="993"
LUMI_EMAIL_SIGNATURE="Lumi - official LumaKit agent"
LUMI_EMAIL_MAX_PER_HOUR="10"
```

Do not commit real credentials. `.env` is ignored already, but verify before pushing.

## Step 5 — Start a surface that boots the service

Recommended:

```bash
lumakit serve
```

For Telegram directly:

```bash
python -m surfaces.telegram
```

For the web backend:

```bash
python -m surfaces.web
```

What matters is that the shared service starts. `core.service.LumaKitService` owns the email checker and starts `EmailChecker(interval=60)` behind the scenes.

## Step 6 — Verify startup

Watch the terminal after boot. On first run with email configured, the checker should seed itself to the current mailbox high-water mark and then only react to newer mail. You should see a line like:

```text
[email checker] seeded last_uid=4217, polling every 60s
```

That means existing inbox mail will be ignored and only mail arriving after startup will trigger notifications.

## Step 7 — Test the full loop

Send a message to Lumi's Gmail address from a different email account. Within about a minute the owner should receive:

- `📧 New email`
- sender and subject
- the URL-scrubbed body
- a casual model-written summary
- an optional `draft reply` block if the message looks reply-worthy

If a draft is included, the owner can answer `yes` to send it or `no` to discard it. On Telegram, that approval is handled by the pending-draft flow in [surfaces/telegram.py](/home/patrick/LumaKit/surfaces/telegram.py). Manual email sends still go through the same safety pipeline and use the surface-specific confirmation UI.

## Inbound safety layers

| Layer | What it does |
|---|---|
| Owner routing | Email notifications go to the owner surface |
| URL stripping | HTML links, script/style content, images, and plaintext URLs are removed before the LLM sees the message |
| Link shelf | Stripped URLs are shown to the owner separately and never passed through the LLM |
| Fallback behavior | If the LLM summary fails, the owner still gets the sanitized raw body |

## Outbound safety layers

| Layer | What it does |
|---|---|
| Owner gate | `email_send`, `email_reply`, `email_check_inbox`, and `email_read` refuse for non-owners |
| Leak scan | Blocks codebase/internal terms before send |
| Rate limit | Rolling per-hour cap from `LUMI_EMAIL_MAX_PER_HOUR` |
| Approval | Requires owner confirmation before sending |
| Signature | Appends `LUMI_EMAIL_SIGNATURE` automatically |
| Audit log | Writes attempts to `.lumakit/sent_emails.log` |

## Manual tools

The owner can also trigger email directly through the agent:

- `email_send`
- `email_check_inbox`
- `email_read`
- `email_reply`

These tools live in [tools/comms/email.py](/home/patrick/LumaKit/tools/comms/email.py) and still enforce owner checks plus the outbound safety pipeline.

## Troubleshooting

**`LUMI_EMAIL_ADDRESS and LUMI_EMAIL_PASSWORD must be set in .env`**
The email config was not loaded. Check `.env` or `~/.lumakit/config.env`, then restart the process.

**`Blocked by codebase-leak filter`**
The outbound draft tripped a pattern in the leak scanner. Check [core/email_filter.py](/home/patrick/LumaKit/core/email_filter.py).

**`Rate limit: hit max emails per hour`**
You've hit the current send cap. Wait or raise `LUMI_EMAIL_MAX_PER_HOUR`.

**`IMAP error: [AUTHENTICATIONFAILED]`**
The Gmail app password is wrong or 2FA is not fully enabled.

**No notifications arrive**
Confirm you started a real surface or service, not just edited env vars. Then check for the checker startup line and verify the test email landed in Inbox instead of Spam.

**The summary/draft never appears**
The fallback path still sends the sanitized body if the model call fails or times out. That's expected behavior when the summarizer misses its deadline.

## Files involved

- [core/service.py](/home/patrick/LumaKit/core/service.py) — starts and wires the shared email worker
- [core/email_checker.py](/home/patrick/LumaKit/core/email_checker.py) — IMAP polling, sanitization, LLM summary, pending draft notifications
- [tools/comms/email.py](/home/patrick/LumaKit/tools/comms/email.py) — manual owner-only email tools and shared send pipeline
- [core/email_filter.py](/home/patrick/LumaKit/core/email_filter.py) — URL stripping, leak scan, rate limit, signature, audit log
- [surfaces/telegram.py](/home/patrick/LumaKit/surfaces/telegram.py) — owner `yes` / `no` handling for pending drafts on Telegram
