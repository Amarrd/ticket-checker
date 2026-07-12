# Eurostar Ticket Watcher — Build Brief

## Goal

Watch a specific Eurostar search-results URL on a schedule and email me the
first time it looks like trains are actually available to book. Runs on
GitHub Actions (free, no server to maintain).

## Context / why this needs a headless browser

The Eurostar search page (e.g.
`https://www.eurostar.com/search/uk-en?origin=...&destination=...&outbound=...&inbound=...`)
is a JS-rendered Next.js app. A plain HTTP GET returns an empty shell —
confirmed by fetching it directly, which returned only placeholder markup
and no fare data. Real availability only appears after the client-side app
loads and calls its own API. So the check needs a real (headless) browser —
this uses Playwright with Chromium.

**Known limitation:** I was not able to render the live page myself while
writing the detection logic, so the "is this page showing available trains"
heuristic (see `NEGATIVE_PHRASES` and the price regex in
`eurostar_watch.py`) is a best-effort guess based on the placeholder copy
the page showed pre-render. **First task for you (Claude Code): after
setting this up, do a manual run, pull the `debug_screenshot.png` and
`debug_page_text.txt` artifacts it produces, and tighten the detection
logic to match what the page actually shows for "no trains" vs. "trains
available" states.**

## How it should work

1. GitHub Actions runs on a schedule (every 30 min) and via manual dispatch.
2. The script loads `SEARCH_URL` in headless Chromium, waits for it to
   render, and takes a screenshot + text dump for debugging.
3. It decides `available: true/false` based on whether a price (`£` +
   digits) appears and none of the "nothing available" phrases are present.
4. State (`available`, `last_checked`) is persisted in `state.json`, which
   the workflow commits back to the repo after every run.
5. An email (via Resend) is sent **only on the transition** from
   not-available → available, so I'm not emailed every 30 minutes while
   tickets remain available.

## Required GitHub repo secrets

- `SEARCH_URL` — the full Eurostar search URL to check
- `RESEND_API_KEY` — API key from resend.com
- `TO_EMAIL` — my email address (must match the Resend account's signup
  email unless a custom sending domain is verified in Resend)
- `FROM_EMAIL` — optional, defaults to `onboarding@resend.dev`

## Project structure to create

```
eurostar-ticket-watcher/
├── .github/
│   └── workflows/
│       └── check.yml
├── eurostar_watch.py
├── requirements.txt
├── state.json
└── README.md
```

## File contents

### `eurostar_watch.py`

```python
"""
Eurostar ticket availability watcher.

Loads a Eurostar search-results URL in a headless browser (the page is a
JS-rendered app, so a plain HTTP request won't show real results), looks for
signals that trains are actually available, and emails you via Resend the
first time it flips from "not available" to "available". State is persisted
in state.json (committed back to the repo by the GitHub Actions workflow) so
you don't get a fresh email every single run while tickets remain available.

Required environment variables:
  SEARCH_URL       Full Eurostar search URL to check
  RESEND_API_KEY   API key from resend.com
  TO_EMAIL         Where to send the alert (must match your Resend account
                    email unless you've verified a custom sending domain)
Optional:
  FROM_EMAIL       Defaults to onboarding@resend.dev
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.async_api import async_playwright

SEARCH_URL = os.environ["SEARCH_URL"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
TO_EMAIL = os.environ["TO_EMAIL"]
FROM_EMAIL = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")

STATE_FILE = Path("state.json")
SCREENSHOT_FILE = Path("debug_screenshot.png")
SNAPSHOT_FILE = Path("debug_page_text.txt")

# Phrases that suggest "nothing to book" on the results page. These are a
# best guess based on the page's placeholder copy — check debug_page_text.txt
# after your first run(s) and adjust this list to match what you actually see.
NEGATIVE_PHRASES = [
    "no trains available",
    "no train options",
    "sold out",
    "fully booked",
    "no availability",
    "options have changed",
    "no results",
    "select a new train",
]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"available": False, "last_checked": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_email(subject: str, body_html: str) -> None:
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": FROM_EMAIL,
            "to": [TO_EMAIL],
            "subject": subject,
            "html": body_html,
        },
        timeout=30,
    )
    resp.raise_for_status()


async def check_availability() -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=45000)
        # Give the client-side app a bit longer to finish hydrating/rendering
        await page.wait_for_timeout(4000)

        await page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)
        text = (await page.inner_text("body")).lower()
        SNAPSHOT_FILE.write_text(text)

        has_negative = any(phrase in text for phrase in NEGATIVE_PHRASES)
        has_price = bool(re.search(r"£\s?\d", text))

        await browser.close()
        return {"available": has_price and not has_negative}


def main() -> None:
    result = asyncio.run(check_availability())
    state = load_state()

    was_available = state.get("available", False)
    is_available = result["available"]

    print(f"Previously available: {was_available}")
    print(f"Now available: {is_available}")

    if is_available and not was_available:
        send_email(
            subject="Eurostar tickets may be available",
            body_html=(
                "<p>The Eurostar search page now shows what looks like "
                "available trains for your route.</p>"
                f"<p><a href='{SEARCH_URL}'>Check it now</a></p>"
                f"<p>Checked at {datetime.now(timezone.utc).isoformat()} UTC</p>"
            ),
        )
        print("Email sent.")

    state["available"] = is_available
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
```

### `requirements.txt`

```
playwright==1.48.0
requests==2.32.3
```

### `.github/workflows/check.yml`

```yaml
name: Eurostar Ticket Watcher

on:
  schedule:
    - cron: "*/30 * * * *"   # every 30 minutes (GitHub may delay this under load)
  workflow_dispatch: {}       # lets you trigger a manual run from the Actions tab

permissions:
  contents: write

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          playwright install --with-deps chromium

      - name: Run availability check
        env:
          SEARCH_URL: ${{ secrets.SEARCH_URL }}
          RESEND_API_KEY: ${{ secrets.RESEND_API_KEY }}
          TO_EMAIL: ${{ secrets.TO_EMAIL }}
          FROM_EMAIL: ${{ secrets.FROM_EMAIL }}
        run: python eurostar_watch.py

      - name: Upload debug artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: debug-output
          path: |
            debug_screenshot.png
            debug_page_text.txt
          retention-days: 3

      - name: Commit updated state
        run: |
          git config user.name "eurostar-watcher-bot"
          git config user.email "actions@users.noreply.github.com"
          git add state.json
          git diff --staged --quiet || git commit -m "Update availability state [skip ci]"
          git push
```

### `state.json`

```json
{
  "available": false,
  "last_checked": null
}
```

### `README.md`

```markdown
# Eurostar Ticket Watcher

Checks a Eurostar search-results URL every 30 minutes via GitHub Actions and
emails you (via Resend) the first time it looks like trains are available.

## How it works

The Eurostar search page is a JS-rendered app — a plain HTTP request doesn't
return real fare data, only a placeholder shell. So this uses a headless
Chromium browser (Playwright) to actually load the page, then looks for a
£ price on the page while checking it isn't showing one of the known
"nothing available" messages.

**Important:** detection logic (`NEGATIVE_PHRASES` in `eurostar_watch.py`)
is a best-effort guess — check the debug artifacts after your first runs and
tune it against what the page actually shows.

## Setup

1. Create a new GitHub repo (private is fine) and push these files.
2. Sign up at resend.com (free tier), grab an API key. Note the signup
   email — the default sender can only deliver to that address unless a
   custom domain is verified.
3. Add repo secrets: `SEARCH_URL`, `RESEND_API_KEY`, `TO_EMAIL`,
   `FROM_EMAIL` (optional).
4. Run the workflow manually first (Actions tab → Run workflow), then check
   the `debug-output` artifact (screenshot + page text) to confirm it's
   reading the page correctly.
5. Once confirmed, leave it running.

## Notes & limits

- GitHub's free scheduled workflows can run a bit late under load.
- Scheduled workflows auto-disable after 60 days of repo inactivity — any
  commit or manual run reactivates them.
- Keep the polling interval reasonable (30 min default) — avoid cranking it
  down much further.
```

## Tasks for Claude Code

1. Create the repo structure and files exactly as above.
2. Init git, help me create the GitHub repo and push (or tell me the exact
   commands if you don't have GitHub access configured).
3. Walk me through adding the four repo secrets.
4. Trigger a manual workflow run, retrieve/inspect the debug artifacts, and
   refine `NEGATIVE_PHRASES` (and the availability heuristic generally)
   based on what the page actually renders — this is the main open item.
5. Confirm the state-commit step works (i.e. `state.json` updates and
   commits cleanly on each run) without creating a commit loop.
