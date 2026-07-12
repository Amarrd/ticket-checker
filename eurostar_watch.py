"""
Eurostar (Snow) ticket availability watcher.

Loads a Eurostar search-results URL in a headless browser (the page is a
JS-rendered Next.js app, so a plain HTTP request only returns an empty shell),
decides whether trains are actually *bookable*, and emails you via Resend the
first time it flips from "not available" to "available". State is persisted in
state.json (committed back to the repo by the GitHub Actions workflow) so you
don't get a fresh email every run while tickets remain available.

Detection is keyed on the page's own stable data-testid hooks, verified live
against both a not-available Snow page and a page with real availability:

  * render anchor   [data-testid="search-results-outbound"]      (page rendered)
  * journey rows    [data-testid="search-results-outbound-item"] (>0 => bookable)
  * empty state     [data-testid="no-train-availability-error"]  (=> not bookable)
  * captcha         [data-testid="captcha-dialog"] / captcha-container (blocked)

  is_available = render_ok and not blocked and journey_rows > 0
                 and not no_availability

A page that fails to render, or is served a captcha wall, is treated as
"unknown"/"blocked" -- never as "available" -- so a bot block can't produce a
false alert (and, in the blocked case, sends a distinct heads-up instead).

Required environment variables:
  SEARCH_URL       Full Eurostar search URL to check
  RESEND_API_KEY   API key from resend.com
  TO_EMAIL         Where to send the alert (must match your Resend account
                    email unless you've verified a custom sending domain)
Optional:
  FROM_EMAIL       Defaults to onboarding@resend.dev

Usage:
  python eurostar_watch.py               # normal scheduled check
  python eurostar_watch.py --dry-run     # check + write artifacts, no email/state
  python eurostar_watch.py --test-email  # send a sample alert now, then exit
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.async_api import async_playwright

STATE_FILE = Path("state.json")
SCREENSHOT_FILE = Path("debug_screenshot.png")
SNAPSHOT_FILE = Path("debug_page_text.txt")

# Selectors grounded on the live page (see module docstring).
RESULTS_ANCHOR = '[data-testid="search-results-outbound"]'
DATEPICKER_ANCHOR = '[data-testid="datePickerWrapper"]'
JOURNEY_ROW = '[data-testid="search-results-outbound-item"]'
NO_AVAILABILITY_ERROR = '[data-testid="no-train-availability-error"]'
CAPTCHA_DIALOG = '[data-testid="captcha-dialog"]'
CAPTCHA_CONTAINER = '[data-testid="captcha-container"]'

# Text fallback for the empty state, in case the testid ever changes.
NEGATIVE_TEXT = "sorry, no trains are available"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "available": False,
        "status": "not_available",
        "last_checked": None,
        "last_change": None,
        "journey_rows": 0,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def _require_env() -> tuple[str, str, str, str]:
    try:
        search_url = os.environ["SEARCH_URL"]
        resend_api_key = os.environ["RESEND_API_KEY"]
        to_email = os.environ["TO_EMAIL"]
    except KeyError as exc:
        sys.exit(f"Missing required environment variable: {exc.args[0]}")
    from_email = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")
    return search_url, resend_api_key, to_email, from_email


def _recipients(to_email: str) -> list[str]:
    """TO_EMAIL may hold several addresses, separated by commas or whitespace."""
    return [addr for addr in re.split(r"[,\s]+", to_email.strip()) if addr]


def send_email(subject: str, body_html: str) -> None:
    _, resend_api_key, to_email, from_email = _require_env()
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {resend_api_key}"},
        json={
            "from": from_email,
            "to": _recipients(to_email),
            "subject": subject,
            "html": body_html,
        },
        timeout=30,
    )
    resp.raise_for_status()


async def check_availability(search_url: str) -> dict:
    """Load the page and classify it. Always writes debug artifacts.

    Returns a dict: {status, is_available, blocked, render_ok, journey_rows}.
    status is one of: available | not_available | unknown | blocked.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-GB",
            viewport={"width": 1400, "height": 1000},
        )
        page = await context.new_page()

        render_ok = False
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            # Wait for the client-side app to render the results region. Fall back
            # to the date picker, which renders in every non-blocked state.
            try:
                await page.wait_for_selector(RESULTS_ANCHOR, timeout=30000)
                render_ok = True
            except Exception:
                render_ok = await page.query_selector(DATEPICKER_ANCHOR) is not None
            # Small settle so late-hydrating rows/errors are in the DOM.
            await page.wait_for_timeout(3000)
        except Exception as exc:
            print(f"Navigation/render error: {exc}")

        # Always capture artifacts, even on a failed/blocked load.
        try:
            await page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)
        except Exception as exc:
            print(f"Screenshot failed: {exc}")
        try:
            body_text = (await page.inner_text("body")).lower()
        except Exception:
            body_text = ""
        SNAPSHOT_FILE.write_text(body_text)

        journey_rows = len(await page.query_selector_all(JOURNEY_ROW))

        captcha_el = await page.query_selector(CAPTCHA_DIALOG)
        captcha_visible = bool(captcha_el and await captcha_el.is_visible())
        # A captcha container gating the page (no results, page didn't render).
        captcha_gating = (
            not render_ok
            and journey_rows == 0
            and await page.query_selector(CAPTCHA_CONTAINER) is not None
        )
        blocked = captcha_visible or captcha_gating

        no_availability = (
            await page.query_selector(NO_AVAILABILITY_ERROR) is not None
            or NEGATIVE_TEXT in body_text
        )

        await browser.close()

    is_available = (
        render_ok and not blocked and journey_rows > 0 and not no_availability
    )

    if blocked:
        status = "blocked"
    elif not render_ok:
        status = "unknown"
    elif is_available:
        status = "available"
    else:
        status = "not_available"

    return {
        "status": status,
        "is_available": is_available,
        "blocked": blocked,
        "render_ok": render_ok,
        "journey_rows": journey_rows,
    }


def _available_email(search_url: str) -> tuple[str, str]:
    return (
        "🎿 Eurostar tickets look available!",
        (
            "<p>The Eurostar search page now shows bookable trains for your "
            "route (real journey rows rendered, no 'no trains available' "
            "message).</p>"
            f"<p><a href='{search_url}'>Book now</a></p>"
            f"<p>Checked at {now_iso()} UTC</p>"
        ),
    )


def _blocked_email(search_url: str) -> tuple[str, str]:
    return (
        "⚠️ Eurostar watcher is being blocked (captcha)",
        (
            "<p>The watcher hit a captcha / bot wall and could not read the "
            "page, so it can't tell whether tickets are available. It will keep "
            "trying, but you may want to check manually.</p>"
            f"<p><a href='{search_url}'>Open the search page</a></p>"
            f"<p>Detected at {now_iso()} UTC</p>"
        ),
    )


def run_check(search_url: str, dry_run: bool) -> int:
    result = asyncio.run(check_availability(search_url))
    state = load_state()

    was_available = state.get("available", False)
    prev_status = state.get("status")
    status = result["status"]
    is_available = result["is_available"]

    print(f"Previous status: {prev_status} (available={was_available})")
    print(f"Current status:  {status} (journey_rows={result['journey_rows']}, "
          f"render_ok={result['render_ok']}, blocked={result['blocked']})")

    if dry_run:
        print("--dry-run: no email sent, state not written.")
        return 0

    # Alert on the transition INTO available (from not_available/unknown).
    if is_available and not was_available:
        subject, body = _available_email(search_url)
        send_email(subject, body)
        print("Availability email sent.")

    # Alert once when we newly become blocked, so failures aren't silent.
    if status == "blocked" and prev_status != "blocked":
        subject, body = _blocked_email(search_url)
        send_email(subject, body)
        print("Blocked-notice email sent.")

    now = now_iso()
    if status != prev_status:
        state["last_change"] = now
    # Never let an unknown/blocked read clobber a known-good availability flag.
    if status in ("available", "not_available"):
        state["available"] = is_available
        state["status"] = status
        state["journey_rows"] = result["journey_rows"]
    else:
        # Record the transient status for visibility without changing `available`.
        state["status"] = status
    state["last_checked"] = now
    save_state(state)
    return 0


def send_test_email() -> int:
    search_url = os.environ.get("SEARCH_URL", "(SEARCH_URL not set)")
    send_email(
        subject="✅ Eurostar watcher — test email",
        body_html=(
            "<p>This is a <strong>test email</strong> from the Eurostar ticket "
            "watcher. If you received this, Resend delivery is working.</p>"
            f"<p>Watching: {search_url}</p>"
            f"<p>Sent at {now_iso()} UTC</p>"
        ),
    )
    print("Test email sent.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Eurostar ticket watcher")
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a sample alert email immediately and exit (no check).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the check and write artifacts, but send no email and don't "
        "update state.json.",
    )
    args = parser.parse_args()

    if args.test_email:
        sys.exit(send_test_email())

    search_url, _, _, _ = _require_env()
    sys.exit(run_check(search_url, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
