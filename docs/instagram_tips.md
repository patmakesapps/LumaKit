# Instagram Navigation Tips for Browser Automation

## Account Info

Credentials (username, email, password) live in `lumi/identity.txt` — gitignored. Read that file before attempting any signup, login, or recovery flow.

## Key Lessons Learned

### Instagram Uses DIVs, Not Buttons
- Instagram's interactive elements (Follow, Follow Back, Review, Accept, etc.) are mostly `<div>` or `<span>` elements, **not** `<button>` elements.
- However, Playwright's `button:has-text()` selector sometimes works even on divs that visually look like buttons — Playwright may match by role/aria or visual semantics.
- `div[role='button']:has-text('Follow')` often DOESN'T exist — Instagram doesn't always set role attributes.
- The most reliable selector is `text=ButtonLabel` (e.g., `text=Review`, `text=Accept`).
- For Follow/Follow Back on the notifications page, `button:has-text('Follow Back')` worked.
- **Key takeaway:** Try `text=Label` first, then `button:has-text('Label')`, and `div[role='button']` last.

### Selectors That Work
- **Follow Back (notifications page):** `button:has-text('Follow Back')`
- **Review collab invite:** `text=Review`
- **Accept collab invite:** `button:has-text('Accept')` (after Review opens the dialog)
- **Navigation:** Direct URL navigation (`https://www.instagram.com/notifications/`) is more reliable than clicking nav links — nav links often don't have standard `<a href>` attributes.
- **Post URLs:** Instagram posts have format `/p/POST_ID/` — use `get_links` to find them.

### Selectors That DON'T Work
- `a[href='/notifications/']` — not a real link on Instagram's SPA
- `div[role='button']:has-text('Follow')` on profile pages — no such element exists
- `div[role='button']:has-text('Accept')` — Accept button isn't a role=button div

### Collaboration Invites — The Flow
1. Navigate to the post URL (find it via `get_links` on notifications page)
2. The post shows "invited you to be a collaborator on their post. Review"
3. Click `text=Review` — this opens a dialog at the bottom of the page
4. The dialog shows: "Accept invite?" with Accept / Decline / Not now
5. Click `button:has-text('Accept')` to accept
6. Verify by checking profile — post count should increase

### Direct Messages
- DM inbox: `https://www.instagram.com/direct/inbox/`
- Clicking on DM threads in the inbox is tricky — div elements don't always respond to click selectors
- Message requests are at `https://www.instagram.com/direct/requests/`
- DM threads don't have standard `<a>` links — they use React-based click handlers

### Notifications
- Navigate directly: `https://www.instagram.com/notifications/`
- Notifications show: who followed, liked, commented, and collab invites
- "Follow Back" button appears next to new follower notifications

### General Tips
- **Always use `get_text` first** to understand what's on the page before trying to click
- **Use `get_links`** to find post URLs and navigation links
- **Wait after SPA transitions** — Instagram is a React SPA, so content renders after JS loads
- **Screenshot often** — send screenshots to Telegram to visually debug when selectors fail
- **Persistent sessions** — use `session_id` parameter to keep login state across calls
- **Post creation** — uses the `/create/` flow with `set_input_files` for photo upload
- **Scroll to top** before interacting with top-of-page elements

### Login Flow (for reference)
1. Navigate to `https://www.instagram.com/accounts/login/`
2. Fill username and password using `type` action (not `fill` — React controlled inputs)
3. Click log in button
4. Handle "Save Login Info" prompt by clicking "Not Now"
5. Handle notifications popup by clicking "Not Now"

### Post Creation Flow
1. Click the "Create" (new post) button
2. Upload image with `set_input_files` action
3. Click "Next" (`text=Next`)
4. Optionally add caption
5. Click "Share" (`text=Share`)
6. Handle share confirmation

### Profile Elements
- Profile URL: `https://www.instagram.com/agent_lumi/`
- Post count, follower count, following count are visible in profile text
- "Edit profile" button visible on own profile