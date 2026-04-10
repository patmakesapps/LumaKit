# Connecting Gmail to Your Agent

This guide walks you through giving Lumi her own Gmail account so she can read, triage, draft, and send email on your behalf — with a safety net that requires one-tap owner approval on Telegram before anything leaves the outbox.

## What you'll get

Once set up, Lumi will:

1. **Poll the inbox every 60 seconds** over IMAP (no email ever sits unread for long)
2. **Strip every URL** out of incoming messages before the LLM sees them (anti-phishing — the LLM cannot be tricked into clicking a malicious link)
3. **Ask the LLM** to summarize each new email in a casual Telegram message, and draft a reply if the email warrants one
4. **Notify you on Telegram** with the full body, summary, optional draft, and the list of links that were stripped out (shown only to you)
5. **Wait for your one-tap approval** — reply `yes` to send the drafted reply, `no` to discard it
6. **Run safety checks** on every outbound message: codebase-leak scanner, rate limit (10/hour by default), signature, and audit log

Email tools are **owner-only** — the first ID in `TELEGRAM_ALLOWED_IDS` is the only one who can trigger sends, replies, or approve drafts. Other household members cannot use email tools at all.

## Step 1 — Create or pick a Gmail account

You probably don't want to use your primary Gmail for this. Create a fresh account (e.g., `yournamelumi@gmail.com`) so Lumi has her own identity and isn't mixing your real mail in with agent traffic.

## Step 2 — Enable 2-Factor Authentication

Gmail won't let you generate an App Password without 2FA on the account.

1. Sign in to the Gmail account
2. Go to https://myaccount.google.com/security
3. Enable **2-Step Verification** (any method — phone, authenticator app, whatever)

## Step 3 — Generate an App Password

App Passwords are 16-character tokens Google gives you for "less secure" apps that can't do full OAuth. They're the right tool here.

1. Go to https://myaccount.google.com/apppasswords
2. App name: type `Lumi` (or anything you want — this is just a label)
3. Click **Create**
4. Google shows you a 16-character password with spaces, like `sqab elap uiko mrdz`
5. Copy it immediately — you won't see it again

> The spaces don't matter. LumaKit strips them when it reads the env var.

## Step 4 — Add credentials to `.env`

Open your `.env` file and set these values:

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

**Never commit `.env`.** The `.gitignore` already excludes it, but double-check before pushing.

## Step 5 — Start the bridge

```bash
python telegram_bridge.py
```

You should see something like:

```
Telegram bridge running. 1 authorized user(s).
[email checker] seeded last_uid=4217, polling every 60s
```

The `seeded last_uid` line means the checker has noted your current inbox high-water mark and will only act on mail that arrives **after** startup. No spam flood on boot.

## Step 6 — Test it

Send an email to Lumi's account from a different address. Within 60 seconds you should get a Telegram message from Lumi with:

- `📧 New email`
- `From:` / `Subject:` / full body
- A casual summary in Lumi's voice
- (If warranted) a `─── draft reply ───` block with the proposed reply and a prompt: **Reply 'yes' to send it, or 'no' to skip.**
- A list of any links that were stripped from the body

Reply `yes` — the draft fires through the full safety pipeline (leak scan → rate limit → signature → audit log → SMTP) and Lumi reports `✅ Sent`. Reply `no` and the draft is discarded.

You can also just ignore the draft and talk to Lumi normally ("actually tell them X instead") — the context has been injected into your agent session, so she knows what email you're referring to.

## Safety layers (inbound)

| Layer | What it does |
|---|---|
| Owner gate | All email tools refuse unless the current Telegram user is the owner |
| URL stripping | `<a href>`, `<img>`, `<script>`, `<style>`, and plain-text URLs are obliterated before the LLM sees the body. Visible anchor text like "Click here" is also stripped so a mismatched href can't fool the model |
| Link shelf | The full URL list is shown **only** on your Telegram notification, never passed through the LLM |

## Safety layers (outbound)

| Layer | What it does |
|---|---|
| Owner gate | All send/reply tools refuse unless the current Telegram user is the owner |
| Codebase-leak scanner | Regex filter blocks outbound mail containing env var names, file paths, tokens, internal names like "lumakit", "ollama", "claude", etc. |
| Rate limit | Rolling 1-hour window, default 10 sends max (configurable via `LUMI_EMAIL_MAX_PER_HOUR`) |
| Owner confirmation | Every send asks for approval before leaving SMTP — either interactively via `cli.confirm`, or via the one-shot `yes/no` flow on pending drafts |
| Signature | Every outbound body has the signature appended automatically |
| Audit log | Every attempt (approved or blocked) is written to `.lumakit/sent_emails.log` as JSON lines |

## Troubleshooting

**"LUMI_EMAIL_ADDRESS and LUMI_EMAIL_PASSWORD must be set in .env"**
The env vars aren't loaded. Check that `.env` is in the project root and that you restarted `telegram_bridge.py` after editing.

**"Blocked by codebase-leak filter"**
The LLM put something the leak scanner doesn't like into the draft (model names, file paths, `lumakit`, etc.). Either rephrase your instruction to the agent, or look at `core/email_filter.py` to see which pattern tripped.

**"Rate limit: hit max emails per hour"**
Lumi has hit the outbound cap. Wait, or bump `LUMI_EMAIL_MAX_PER_HOUR` in `.env`.

**"IMAP error: [AUTHENTICATIONFAILED]"**
Your app password is wrong. Regenerate it at https://myaccount.google.com/apppasswords and paste the new 16-char value into `.env`.

**Email checker is silent — no notifications arrive**
Check the terminal running `telegram_bridge.py`. You should see `[email checker] seeded last_uid=N, polling every 60s` at startup. If you don't, the checker didn't start — look for exceptions above that line. If it did start but nothing fires when mail arrives, verify you're sending to the exact address in `LUMI_EMAIL_ADDRESS` and that Gmail didn't drop the incoming mail into Spam.

**LLM times out during triage**
The side-channel LLM call has a 90-second deadline. If your model is slow, Lumi will still fall back to showing you the raw (URL-stripped) body — you just won't get the summary or draft. Consider a faster model for the primary.

## Manual tools

In addition to the background checker, the agent has these tools available (all owner-only):

- `email_send` — compose and send a brand-new email
- `email_check_inbox` — list recent messages (metadata only)
- `email_read` — read one message by id (URLs stripped)
- `email_reply` — reply to an existing message by id

You can ask the agent things like "email Bob and tell him I'll be late" or "check my inbox" and it'll route through these tools, still subject to owner approval before anything sends.

## Files involved

- `core/email_checker.py` — the 60s polling loop, LLM triage, pending-draft state
- `core/email_filter.py` — URL stripper, leak scanner, rate limiter, audit log, signature
- `core/auth.py` — owner gate (`is_owner_active()`)
- `tools/comms/email.py` — the manual email tools and `send_preapproved()` helper
- `telegram_bridge.py` — wires the checker to Telegram, intercepts yes/no for pending drafts
