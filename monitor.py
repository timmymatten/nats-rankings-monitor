#!/usr/bin/env python3
"""Monitor the NATS (USA Roundnet) Glicko-2 rankings and notify Slack on change.

Scrapes the dynamically-rendered Shiny rankings app with Playwright across both the
open and women's divisions, then:
  - posts a public "rankings updated" message to a Slack channel (with shoutouts for
    roster members who got promoted a tier or crossed 1000 / "contender" in open), and
  - DMs each roster member their own rating/rank change.

State (a per-player snapshot keyed by name+division, plus a top-20 fingerprint that
drives the general-update + pause logic) lives in snapshot.json. All Slack messaging
uses a bot token (DMs are impossible with an Incoming Webhook).

On any scrape failure the script prints the error and exits 0 without touching the
snapshot — a transient failure should never fire a false notification or fail the
GitHub Action. Tournaments are Saturday-only, so after a notification the monitor
pauses checks until the following Saturday.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# Tournaments only happen on Saturdays, so after a notification there's nothing to
# look for until the next Saturday. TIMEZONE defines when that Saturday begins.
TIMEZONE = "America/New_York"
SATURDAY = 5  # datetime.weekday(): Monday=0 … Saturday=5

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


def scrape_division(page, selector):
    """Return {NORMALIZED_NAME: {name, rank, rating, rating_str, tier}} for one table.

    Sets the page size to 100 and clicks through every page. Columns are stable:
    Name, Rank, Change, Status(tier), Status Expires, Rating, ...
    """
    page.wait_for_selector(f"{selector} tbody tr td", timeout=PAGE_TIMEOUT_MS)

    length_select = page.query_selector(f"{selector} select")
    if length_select:
        try:
            length_select.select_option("100")
            page.wait_for_timeout(800)
        except Exception:  # noqa: BLE001 — fall back to the default page size
            pass

    players = {}
    seen_pages = 0
    while True:
        for row in page.query_selector_all(f"{selector} tbody tr"):
            cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
            if len(cells) < 6:
                continue
            name, rank_s, tier, rating_s = cells[0], cells[1], cells[3], cells[5]
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
        seen_pages += 1

        nxt = page.query_selector(f"{selector} .paginate_button.next")
        classes = (nxt.get_attribute("class") or "") if nxt else "disabled"
        if not nxt or "disabled" in classes:
            break
        nxt.click()
        page.wait_for_timeout(250)
        if seen_pages > 60:  # safety stop well beyond the real page count
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
    """Top-N open-division 'NAME|rating' list — the general-update + pause trigger."""
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


def _local_timezone():
    """Return the configured tz, falling back to UTC on minimal runners."""
    try:
        return ZoneInfo(TIMEZONE)
    except (ZoneInfoNotFoundError, KeyError):
        return timezone.utc


def next_saturday(now):
    """Aware datetime at 00:00 (TIMEZONE) for the next (strictly future) Saturday."""
    local = now.astimezone(_local_timezone())
    days = (SATURDAY - local.weekday()) % 7
    if days == 0:  # today is Saturday → the *following* Saturday
        days = 7
    return (local + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def is_paused(snapshot, now):
    """True if the snapshot's pause window is still in the future as of `now`."""
    paused_until = snapshot.get("checks_paused_until")
    if not paused_until:
        return False
    try:
        return now < datetime.fromisoformat(paused_until)
    except ValueError:
        return False  # unparseable → treat as not paused, resume checking


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

    # Saturday-only pause: bail before launching the browser inside the window.
    # FORCE_CHECK (workflow_dispatch "force" input) overrides it.
    force = os.environ.get("FORCE_CHECK", "").lower() in ("1", "true", "yes")
    if not force and is_paused(snapshot, now):
        print(
            f"Checks paused until {snapshot['checks_paused_until']} "
            f"(tournaments are Saturdays); skipping."
        )
        return 0

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
    # establish the baseline silently, no notifications, no pause.
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

    # A real change means a tournament happened — pause until the next Saturday.
    paused_until = next_saturday(now)
    new_snapshot["checks_paused_until"] = paused_until.isoformat()
    save_snapshot(new_snapshot)
    print(f"Snapshot updated. Pausing checks until {paused_until.isoformat()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
