#!/usr/bin/env python3
"""Monitor the NATS (USA Roundnet) Glicko-2 rankings and notify Slack on change.

Scrapes the dynamically-rendered Shiny rankings app with Playwright, fingerprints
the top 20 open-division players (name + rating), and posts a static message to a
Slack Incoming Webhook whenever that fingerprint changes. State lives in
snapshot.json.

On any failure (page won't load, table never appears, etc.) the script prints the
error and exits 0 without touching the snapshot — a transient scrape failure should
never trigger a false notification or fail the GitHub Action.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

RANKINGS_URL = "https://jmhyman.shinyapps.io/USAR-Rankings/"
PUBLIC_RANKINGS_URL = "https://www.usaroundnet.org/rankings"
SNAPSHOT_PATH = Path(__file__).with_name("snapshot.json")

PAGE_TIMEOUT_MS = 30_000  # 30 second timeout on page load / waits
TOP_N = 20
# Open division lives in the #player container; women's is in #playerW. Scope to
# #player so we never accidentally fingerprint the women's table (they update at
# the same time, so reading the wrong one would fire a false "updated" alert).
OPEN_TABLE_SELECTOR = "#player"


def load_snapshot():
    """Return the stored snapshot dict, or {} if missing/unreadable."""
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_snapshot(snapshot):
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)
        fh.write("\n")


def extract_fingerprint(page):
    """Return a list of "name|rating" strings for the top N OPEN-division players.

    The page renders multiple DataTables (open in the #player container, women's
    in #playerW). DataTables' auto-assigned table ids (DataTables_Table_0/_1) are
    ordered by init time and are NOT stable, so we must scope to the semantic
    #player container to always read the open division. Each row contributes a
    name and the first decimal-looking number in that row (the Glicko-2 rating).
    """
    rows = page.query_selector_all(f"{OPEN_TABLE_SELECTOR} tbody tr")
    fingerprint = []
    for row in rows:
        cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
        if not cells:
            continue
        name = next((c for c in cells if re.search(r"[A-Za-z]", c)), "")
        rating = ""
        for c in cells:
            m = re.search(r"\d+\.\d+", c)
            if m:
                rating = m.group(0)
                break
        if not rating:  # fall back to any integer in the row
            for c in cells:
                m = re.search(r"\b\d{3,4}\b", c)
                if m:
                    rating = m.group(0)
                    break
        if name and rating:
            fingerprint.append(f"{name}|{rating}")
        if len(fingerprint) >= TOP_N:
            break
    return fingerprint


def scrape():
    """Return the top-N fingerprint. Raises on failure."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(RANKINGS_URL, timeout=PAGE_TIMEOUT_MS, wait_until="load")
            # Wait for the OPEN-division table rows specifically (not just any
            # table) before extracting.
            page.wait_for_selector(
                f"{OPEN_TABLE_SELECTOR} tbody tr td", timeout=PAGE_TIMEOUT_MS
            )
            fingerprint = extract_fingerprint(page)
            if not fingerprint:
                raise RuntimeError("No ranking rows could be extracted from the page")
            return fingerprint
        finally:
            browser.close()


def post_to_slack(webhook_url):
    message = (
        f"\U0001F3D0 NATS Rankings updated!\n"
        f"Check them out: {PUBLIC_RANKINGS_URL}"
    )
    resp = requests.post(webhook_url, json={"text": message}, timeout=30)
    resp.raise_for_status()


def main():
    try:
        fingerprint = scrape()
    except Exception as exc:  # noqa: BLE001 — any scrape failure is a silent skip
        print(f"Scrape failed, skipping run without touching snapshot: {exc}")
        return 0

    snapshot = load_snapshot()
    previous = snapshot.get("fingerprint")

    if previous == fingerprint:
        print("Rankings unchanged since last run; nothing to do.")
        return 0

    print("Rankings changed (or first run).")

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    # Only notify when we have a prior baseline to compare against. The first run
    # establishes the baseline silently rather than firing a spurious alert.
    if previous is not None:
        if webhook_url:
            try:
                post_to_slack(webhook_url)
                print("Posted Slack notification.")
            except Exception as exc:  # noqa: BLE001
                print(f"Failed to post to Slack: {exc}")
                # Still update the snapshot so we don't repeatedly retry/notify.
        else:
            print("SLACK_WEBHOOK_URL not set; skipping Slack notification.")
    else:
        print("First run — establishing baseline snapshot without notifying.")

    snapshot = {
        "fingerprint": fingerprint,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_snapshot(snapshot)
    print("Snapshot updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
