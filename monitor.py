#!/usr/bin/env python3
"""Monitor the NATS (USA Roundnet) Glicko-2 rankings and notify Slack on change.

Scrapes the dynamically-rendered Shiny rankings app with Playwright across both the
open and women's divisions, then:
  - posts a public "rankings updated" message to a Slack channel (with shoutouts for
    roster members who got promoted a tier or crossed 1000 / "contender" in open), and
  - DMs each roster member their own rating/rank change.

State (a per-player snapshot keyed by name+division, plus a top-20 fingerprint that
drives the general-update trigger) lives in snapshot.json. All Slack messaging uses
a bot token (DMs are impossible with an Incoming Webhook).

On any scrape failure the script prints the error and exits 0 without touching the
snapshot — a transient failure should never fire a false notification or fail the
GitHub Action.
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
ROSTER_PATH = Path(__file__).with_name("roster.json")

PAGE_TIMEOUT_MS = 30_000  # 30 second timeout on page load / waits
TOP_N = 20  # size of the open-division fingerprint used as the general-update trigger

# Open division is the #player container (men + women); women's is #playerW
# (women only). A woman can appear in both and is tracked independently per division.
DIVISIONS = {"open": "#player", "women": "#playerW"}
DIVISION_LABEL = {"open": "Open", "women": "Women's"}

# Tier ladder read straight from the page's "Status" column. Rating ranges overlap
# across tiers, so tier is NEVER computed from rating — only the label is compared.
TIER_ORDER = ["Unranked", "Bronze", "Silver", "Gold", "Pro"]
# "Contender" is a milestone (rating crossing 1000), distinct from the tier label,
# and is only announced for the OPEN division.
CONTENDER_THRESHOLD = 1000.0

SLACK_API = "https://slack.com/api"


# --------------------------------------------------------------------------- state


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


def normalize_name(name):
    """Uppercase + collapse whitespace, for matching roster names to page names."""
    return " ".join(name.split()).upper()


def load_roster():
    """Return roster entries as [{name_norm, divisions, slack_id}], or [] if none.

    Tolerates a missing/empty/invalid file: the feature degrades to the general
    channel message only (no DMs, no shoutouts).
    """
    try:
        with open(ROSTER_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh) or []
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    roster = []
    for entry in raw:
        name = entry.get("name")
        if not name:
            continue
        divisions = [d for d in entry.get("divisions", []) if d in DIVISIONS]
        if not divisions:
            continue
        roster.append(
            {
                "name_norm": normalize_name(name),
                "divisions": divisions,
                "slack_id": entry.get("slack_id"),
            }
        )
    return roster


# ------------------------------------------------------------------------ scraping


def _first_cell(page, selector):
    """Text of the current page's first body cell (the rank-1 name), or '' if none."""
    el = page.query_selector(f"{selector} tbody tr td")
    return el.inner_text().strip() if el else ""


# Body columns are read by HEADER LABEL, not by fixed index, so the scrape survives
# columns being inserted/reordered upstream (e.g. a "Contender Status" column was added
# between "Status Expires" and "Rating" in July 2026, which silently shifted Rating and
# broke the old cells[5] parser). "Status" is the tier label; "Rating" is the numeric
# rating. We only need these four; extra columns are ignored.
REQUIRED_COLUMNS = ("Name", "Rank", "Status", "Rating")


def _column_index(page, selector):
    """Map header label → column index from the table's first header row.

    Raises RuntimeError if any REQUIRED_COLUMNS is missing — a loud, diagnostic
    failure (surfaced via the scrape-failure log) rather than a silently empty scrape.
    """
    header_row = page.query_selector(f"{selector} thead tr")
    ths = header_row.query_selector_all("th") if header_row else []
    cols = {}
    for i, th in enumerate(ths):
        label = th.inner_text().strip()
        if label and label not in cols:  # keep first occurrence; skip spacer columns
            cols[label] = i
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise RuntimeError(
            f"Rankings table for {selector} is missing expected column(s) {missing}; "
            f"saw headers {list(cols)}"
        )
    return cols


def scrape_division(page, selector):
    """Return {NORMALIZED_NAME: {name, rank, rating, rating_str, tier}} for one table.

    Sets the page size to 100 and clicks through every page. Between page clicks it
    waits for the table to actually re-render (the first row changes) rather than a
    fixed sleep — shinyapps.io can be slow under load, and a too-short sleep silently
    truncates the scrape to page 1. Columns are located by header label (see
    _column_index) so upstream column insertions/reorders don't break the parser.
    """
    page.wait_for_selector(f"{selector} tbody tr td", timeout=PAGE_TIMEOUT_MS)

    cols = _column_index(page, selector)
    name_i, rank_i, tier_i, rating_i = (
        cols["Name"],
        cols["Rank"],
        cols["Status"],
        cols["Rating"],
    )
    min_cells = max(name_i, rank_i, tier_i, rating_i) + 1

    # Total entries from the "Showing 1 to X of N entries" label, so we can wait for
    # the 100-row page to *fully* render before harvesting (a too-eager wait silently
    # truncates page 1).
    total = None
    info = page.query_selector(f"{selector} .dataTables_info")
    if info:
        match = re.search(r"of\s+([\d,]+)\s+entries", info.inner_text())
        if match:
            total = int(match.group(1).replace(",", ""))

    length_select = page.query_selector(f"{selector} select")
    if length_select:
        try:
            length_select.select_option("100")
            target = min(100, total) if total else 100
            page.wait_for_function(
                "([sel, t]) => document.querySelectorAll(sel + ' tbody tr').length >= t",
                arg=[selector, target],
                timeout=PAGE_TIMEOUT_MS,
            )
        except Exception:  # noqa: BLE001 — fall back to the default page size
            pass

    players = {}
    for _page in range(60):  # safety stop well beyond the real page count
        for row in page.query_selector_all(f"{selector} tbody tr"):
            cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
            if len(cells) < min_cells:
                continue
            name, rank_s, tier, rating_s = (
                cells[name_i],
                cells[rank_i],
                cells[tier_i],
                cells[rating_i],
            )
            try:
                rank = int(rank_s.replace(",", ""))
                rating = float(rating_s.replace(",", ""))
            except ValueError:
                continue
            if not name:
                continue
            players[normalize_name(name)] = {
                "name": name,
                "rank": rank,
                "rating": rating,
                "rating_str": rating_s,
                "tier": tier,
            }

        nxt = page.query_selector(f"{selector} .paginate_button.next")
        classes = (nxt.get_attribute("class") or "") if nxt else "disabled"
        if not nxt or "disabled" in classes:
            break
        before = _first_cell(page, selector)
        nxt.click()
        # Wait until the first row actually changes (page re-rendered), up to ~12s.
        try:
            page.wait_for_function(
                "([sel, prev]) => {"
                " const el = document.querySelector(sel + ' tbody tr td');"
                " return el && el.innerText.trim() !== prev; }",
                arg=[selector, before],
                timeout=12_000,
            )
        except Exception:  # noqa: BLE001 — page didn't advance; stop paginating
            break
    return players


def scrape():
    """Return {"open": {...}, "women": {...}} player maps. Raises on failure."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(RANKINGS_URL, timeout=PAGE_TIMEOUT_MS, wait_until="load")
            result = {}
            for division, selector in DIVISIONS.items():
                result[division] = scrape_division(page, selector)
            if not result["open"]:
                raise RuntimeError("No open-division rows could be extracted")
            return result
        finally:
            browser.close()


def top_fingerprint(open_players):
    """Top-N open-division 'NAME|rating' list — the general-update trigger."""
    rows = sorted(open_players.values(), key=lambda r: r["rank"])[:TOP_N]
    return [f"{r['name']}|{r['rating_str']}" for r in rows]


def build_player_records(scraped, roster):
    """Map each roster (member, division) to its scraped record.

    Returns {"NAME|division": {division, slack_id, name, rank, rating, tier}}.
    A roster member tracked in both divisions yields two independent keys.
    """
    records = {}
    for entry in roster:
        for division in entry["divisions"]:
            rec = scraped.get(division, {}).get(entry["name_norm"])
            if not rec:
                print(
                    f"Roster member {entry['name_norm']} not found in "
                    f"{division} rankings; skipping that record."
                )
                continue
            records[f"{entry['name_norm']}|{division}"] = {
                "division": division,
                "slack_id": entry.get("slack_id"),
                "name": rec["name"],
                "rank": rec["rank"],
                "rating": rec["rating"],
                "tier": rec["tier"],
            }
    return records


# ----------------------------------------------------------------- change detection


def _tier_index(tier):
    """Ladder position; unknown labels sort to the bottom so only real promotions fire."""
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else 0


def _fmt(x):
    """Compact number formatting: 2368.15, 2205.7, 1478."""
    return f"{x:g}"


def diff_players(prev, curr):
    """Compare per-(name, division) records → (dms, shoutouts).

    dms: one item per changed (member, division), each carrying its composed text and
    slack_id (may be None → shoutout-only, not sent). shoutouts: public lines for
    promotions (both divisions) and contender crossings (open only). Upward only —
    demotions and first-seen records produce nothing.
    """
    dms, shoutouts = [], []
    for key, c in curr.items():
        p = prev.get(key)
        if not p:
            continue  # first-seen → no baseline to diff against

        division, name = c["division"], c["name"]
        rating_changed = float(c["rating"]) != float(p.get("rating"))
        rank_changed = c["rank"] != p.get("rank")
        promoted = _tier_index(c["tier"]) > _tier_index(p.get("tier"))
        contender = (
            division == "open"
            and float(p.get("rating")) < CONTENDER_THRESHOLD <= float(c["rating"])
        )

        if rating_changed or rank_changed:
            dms.append(
                {
                    "slack_id": c.get("slack_id"),
                    "name": name,
                    "division": division,
                    "text": compose_dm(division, p, c, promoted, contender),
                }
            )

        label = DIVISION_LABEL[division]
        display = name.title()
        if promoted:
            shoutouts.append(
                {
                    "name": name,
                    "division": division,
                    "text": f" • {display} — promoted to {c['tier']} ({label})!",
                }
            )
        if contender:
            shoutouts.append(
                {
                    "name": name,
                    "division": division,
                    "text": f" • {display} — reached Contender (crossed 1000) in {label}!",
                }
            )
    return dms, shoutouts


# -------------------------------------------------------------------- message text


def compose_dm(division, prev, curr, promoted, contender):
    delta = float(curr["rating"]) - float(prev["rating"])
    lines = [
        f"\U0001F3D0 Your NATS {DIVISION_LABEL[division]} ranking changed!",
        f"Rating: {_fmt(prev['rating'])} → {_fmt(curr['rating'])} ({delta:+g})",
        f"Rank: {prev['rank']} → {curr['rank']}",
    ]
    if promoted:
        lines.append(f"\U0001F389 Promoted to {curr['tier']}!")
    if contender:
        lines.append("\U0001F389 You're now Contender qualified (crossed 1000)!")
    return "\n".join(lines)


def compose_channel_message(shoutouts):
    lines = ["\U0001F3D0 NATS Rankings updated!"]
    if shoutouts:
        lines.append("\U0001F389 Shoutouts:")
        lines.extend(s["text"] for s in shoutouts)
    lines.append(f"Check them out: {PUBLIC_RANKINGS_URL}")
    return "\n".join(lines)


# --------------------------------------------------------------------- slack client


def slack_call(method, token, payload):
    resp = requests.post(
        f"{SLACK_API}/{method}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data.get('error')}")
    return data


def post_channel_message(token, channel_id, text):
    slack_call("chat.postMessage", token, {"channel": channel_id, "text": text})


def send_dm(token, user_id, text):
    opened = slack_call("conversations.open", token, {"users": user_id})
    channel = opened["channel"]["id"]
    slack_call("chat.postMessage", token, {"channel": channel, "text": text})


# ---------------------------------------------------------------------------- main


def _report_intent(channel_text, dms):
    """Print exactly what would be sent (used both for dry runs and as a run log)."""
    print("\n--- channel message ---")
    print(channel_text if channel_text else "(no public post this run)")
    print("\n--- direct messages ---")
    if not dms:
        print("(none)")
    for dm in dms:
        target = dm["slack_id"] or "NO slack_id (shoutout-only, not sent)"
        print(f"-> {dm['name']} [{DIVISION_LABEL[dm['division']]}] ({target}):")
        print(dm["text"])
    print("--- end ---\n")


def main():
    now = datetime.now(timezone.utc)
    snapshot = load_snapshot()
    roster = load_roster()

    try:
        scraped = scrape()
    except Exception as exc:  # noqa: BLE001 — any scrape failure is a silent skip
        print(f"Scrape failed, skipping run without touching snapshot: {exc}")
        return 0

    fingerprint = top_fingerprint(scraped["open"])
    players = build_player_records(scraped, roster)
    new_snapshot = {
        "top_fingerprint": fingerprint,
        "players": players,
        "updated_at": now.isoformat(),
    }

    # First run under this schema (also catches the legacy {fingerprint,…} snapshot):
    # establish the baseline silently, no notifications.
    if "top_fingerprint" not in snapshot:
        save_snapshot(new_snapshot)
        print("First run — established baseline snapshot without notifying.")
        return 0

    prev_players = snapshot.get("players", {})
    top_changed = fingerprint != snapshot.get("top_fingerprint")
    players_changed = players != prev_players
    if not (top_changed or players_changed):
        print("Rankings unchanged since last run; nothing to do.")
        return 0

    print("Rankings changed.")
    dms, shoutouts = diff_players(prev_players, players)
    # Avoid channel noise on pure rank reshuffles: only post publicly when the top
    # of the rankings actually moved or there's a milestone to celebrate.
    post_public = top_changed or bool(shoutouts)
    channel_text = compose_channel_message(shoutouts) if post_public else None

    # Optional prefix (e.g. "TEST ") for distinguishing test runs in Slack.
    prefix = os.environ.get("MESSAGE_PREFIX", "")
    if prefix:
        if channel_text:
            channel_text = prefix + channel_text
        for dm in dms:
            dm["text"] = prefix + dm["text"]

    _report_intent(channel_text, dms)

    token = os.environ.get("SLACK_BOT_TOKEN")
    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    if token:
        if channel_text:
            if channel_id:
                try:
                    post_channel_message(token, channel_id, channel_text)
                    print("Posted channel message.")
                except Exception as exc:  # noqa: BLE001
                    print(f"Failed to post channel message: {exc}")
            else:
                print("SLACK_CHANNEL_ID not set; skipping channel message.")
        for dm in dms:
            if not dm["slack_id"]:
                continue
            try:
                send_dm(token, dm["slack_id"], dm["text"])
                print(f"Sent DM to {dm['name']} [{DIVISION_LABEL[dm['division']]}].")
            except Exception as exc:  # noqa: BLE001
                print(f"Failed to DM {dm['name']}: {exc}")
    else:
        print("SLACK_BOT_TOKEN not set; skipped sending (intent shown above).")

    save_snapshot(new_snapshot)
    print("Snapshot updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
