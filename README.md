# Eurostar Ticket Watcher

Checks a Eurostar search-results URL every 30 minutes via GitHub Actions and
emails you (via [Resend](https://resend.com)) the first time trains actually
become **bookable**. Built for watching seasonal *Eurostar Snow* dates that
aren't on sale yet, but works for any Eurostar route/date.

## How detection works

The Eurostar search page is a JS-rendered app — a plain HTTP request only returns
an empty shell — so this uses a headless Chromium browser (Playwright) to load
the page for real. It then classifies the page using Eurostar's own stable
`data-testid` hooks (verified against both a not-yet-on-sale page and a page with
real availability):

| Signal | Not available | Available |
| --- | --- | --- |
| `search-results-outbound` (render anchor) | present | present |
| `search-results-outbound-item` (journey rows) | 0 | > 0 |
| `no-train-availability-error` | present | absent |

```
is_available = render_ok and not blocked and journey_rows > 0 and not no_availability
```

Requiring **real journey rows** *and* the **absence of the empty-state error**
avoids the classic false-fire: Snow pages already render times and £ prices
before the sale opens, so "there's a price on the page" is *not* a reliable
signal on its own.

Failure modes are handled conservatively so you never get a false alert:

- **Page didn't render** → status `unknown`; no email, prior state preserved.
- **Captcha / bot wall** (`captcha-dialog` / `captcha-container`) → status
  `blocked`; sends a distinct one-time "watcher is being blocked" heads-up rather
  than silently reading it as "not available".

An email is sent only on the **transition into** available, so you aren't emailed
every 30 minutes while tickets remain available.

State (`available`, `status`, `last_checked`, `last_change`, `journey_rows`) is
persisted in `state.json`, committed back to the repo by the workflow after each
run (with `[skip ci]` so it doesn't trigger itself).

## Setup

1. **Create a GitHub repo** (private is fine) and push these files.
2. **Sign up at [resend.com](https://resend.com)** (free tier) and create an API
   key. Note the email you signed up with — with the default sender
   (`onboarding@resend.dev`) Resend can only deliver to *that* address unless you
   verify a custom sending domain.
3. **Add repo secrets** (Settings → Secrets and variables → Actions → New
   repository secret):
   - `SEARCH_URL` — the full Eurostar search URL to watch
   - `RESEND_API_KEY` — your Resend API key
   - `TO_EMAIL` — where to send alerts. One address, or several separated by
     commas (e.g. `me@example.com, partner@example.com`). Note: with the default
     `onboarding@resend.dev` sender, Resend only delivers to your Resend signup
     address — to reach a second recipient you'll need to verify a custom sending
     domain in Resend.
   - `FROM_EMAIL` — *optional*, defaults to `onboarding@resend.dev`
4. **Test the email path now** (before tickets drop) — see below.
5. **Confirm you're not bot-blocked**: Actions → *Eurostar Ticket Watcher* → *Run
   workflow* (leave "test email" unticked). When it finishes, open the
   `debug-output` artifact and check `debug_screenshot.png` shows the real search
   page, not a captcha.
6. Once confirmed, leave it running on the schedule.

## Testing the email path (before tickets are available)

**From GitHub:** Actions → *Eurostar Ticket Watcher* → *Run workflow* → tick
**"Send a test email instead of checking availability"** → Run. This exercises
the full Actions → secrets → Resend path and sends you a sample email.

**Locally:**

```bash
pip install -r requirements.txt
playwright install chromium

export SEARCH_URL='https://www.eurostar.com/search/uk-en?...'
export RESEND_API_KEY='re_...'
export TO_EMAIL='you@example.com'
# export FROM_EMAIL='alerts@yourdomain.com'   # optional

python eurostar_watch.py --test-email   # send a sample alert now
python eurostar_watch.py --dry-run      # run the real check, no email/state write
```

`--dry-run` prints the classification and writes `debug_screenshot.png` /
`debug_page_text.txt` so you can confirm it reads the page correctly. Point
`SEARCH_URL` at a date you know is on sale to see it report `status: available`.

## Notes & limits

- GitHub's free scheduled workflows can run late under load.
- Scheduled workflows auto-disable after 60 days of repo inactivity — any commit
  or manual run reactivates them.
- Keep the polling interval reasonable (30 min default). Cranking it much lower
  risks tripping Eurostar's bot protection.
