# NATS Rankings Monitor

This project watches the [NATS (USA Roundnet) Glicko-2 rankings](https://jmhyman.shinyapps.io/USAR-Rankings/) and, when they change, **posts a public Slack message** (celebrating roster members who got promoted or made contender) and **DMs roster members their personal rating/rank change**. A GitHub Actions workflow runs every 30 minutes: it uses Playwright to scrape the JavaScript-rendered Shiny app across **both** the open and women's divisions, compares against `snapshot.json`, and messages Slack via a **bot token**. If nothing changed or the page fails to load, it exits quietly and leaves the snapshot untouched.

> **Divisions:** the page renders separate tables — open in the `#player` container (men + women), women's in `#playerW` (women only). The scraper reads both by their stable container IDs (DataTables' auto-assigned table IDs are *not* stable). A player who appears in both divisions is tracked independently in each.

## Who gets notified — roster, DMs, and shoutouts

Notifications are scoped to a **committed roster** (`roster.json`) of people you care about — not all ~1,800 players.

```json
[
  { "name": "JANE DOE",    "divisions": ["open", "women"], "slack_id": "U0123ABC" },
  { "name": "JOHN SMITH",  "divisions": ["open"],          "slack_id": "U0456DEF" },
  { "name": "NO SLACK GUY", "divisions": ["open"] }
]
```

- `name` — matched case-insensitively against the rankings page (names there are uppercase).
- `divisions` — which tables to track this person in. A woman who plays both gets a **separate DM (and shoutout) per division**.
- `slack_id` — optional. With it, the person gets **DMs**; without it, they still appear in public **shoutouts** but receive no DM.

What fires on a change:
- **DM** to each roster member whose **rating or rank** changed, e.g.:
  > 🏐 Your NATS Open ranking changed!
  > Rating: 1450 → 1478 (+28)
  > Rank: 320 → 305
  > 🎉 Promoted to Silver!
- **Public channel message** with shoutouts for roster members who were **promoted a tier** (`Unranked→Bronze→Silver→Gold→Pro`, read from the page's Status column) or **made contender** (crossed 1000 rating — **open division only**):
  > 🏐 NATS Rankings updated!
  > 🎉 Shoutouts:
  >  • Jane Doe — promoted to Gold (Open)!
  >  • John Smith — reached Contender (crossed 1000) in Open!
  > Check them out: https://www.usaroundnet.org/rankings

Everything is **upward only** — demotions are never reported. Tiers come straight from the page's Status label (rating ranges overlap across tiers, so tier is never inferred from rating).

## Pausing between tournaments

NATS tournaments only happen on **Saturdays**, so once a change is detected, no new results appear until the next Saturday. To avoid pointless scraping, the monitor **pauses after a notification until the following Saturday**: it records a `checks_paused_until` timestamp and, while inside that window, each run exits *before* launching the browser. The `TIMEZONE` constant in `monitor.py` (default `America/New_York`) defines when Saturday begins. The first-run baseline never pauses.

> If a *correction* is posted later in the same weekend after a notification fired, it won't be picked up until the next Saturday — consistent with the Saturday-only tournament model.

## Deploy

1. **Fork/clone this repo** (the `rankings_monitor/` contents must sit at the repository root, since `.github/workflows/` only runs from the root).
2. **Set up the Slack bot** (next section) and get the bot token + channel ID.
3. **Add two repository secrets** (**Settings → Secrets and variables → Actions → New repository secret**):
   - `SLACK_BOT_TOKEN` — the bot's `xoxb-…` token
   - `SLACK_CHANNEL_ID` — the `C…` ID of the channel for the public message
4. **Fill `roster.json`** with the people you want to track (see above). Use `tools/list_slack_users.py` to look up member IDs.
5. **Enable workflows** in the **Actions** tab if prompted.
6. **Done.** Runs every 30 minutes. The first run establishes a baseline silently (no messages); after that it only notifies on real changes.

## Setting up the Slack bot

DMs are impossible with an Incoming Webhook, so this uses a bot token.

1. Go to <https://api.slack.com/apps> → **Create New App → From scratch**, name it (e.g. "NATS Rankings Bot"), pick your workspace.
2. **OAuth & Permissions → Scopes → Bot Token Scopes**, add:
   - `chat:write` — post the channel message and DMs
   - `im:write` — open a DM with each person
   - `users:read` — lets the helper script list members
3. **Install to Workspace → Allow.** Copy the **Bot User OAuth Token** (`xoxb-…`) → this is `SLACK_BOT_TOKEN`.
4. **Invite the bot to your channel:** in Slack, `/invite @YourBot` in the target channel (the bot must be a member to post).
5. **Get the channel ID:** click the channel name → bottom of the **About** tab shows **Channel ID** (`C…`), or take the `C…` from the channel URL. This is `SLACK_CHANNEL_ID`.

### Finding member IDs for the roster

```bash
# List everyone, or pass SLACK_CHANNEL_ID to narrow to one channel's members
SLACK_BOT_TOKEN=xoxb-... python tools/list_slack_users.py
SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL_ID=C0123ABCD python tools/list_slack_users.py
```
Prints `Real Name -> U-ID`. (Channel-narrowing also needs a `channels:read`/`groups:read` scope; without it the script lists the whole workspace.) You can also copy an ID manually from a person's Slack profile → **⋮ More → Copy member ID**.

## Manually triggering a run

**Actions** tab → **NATS Rankings Monitor** → **Run workflow** → pick the branch → **Run workflow**. If checks are paused between tournaments, toggle **Bypass the until-Saturday pause and check now** on to force a check.

## Adjusting the schedule

The cadence is the `cron` line in `.github/workflows/monitor.yml`:

```yaml
- cron: "*/30 * * * *"   # every 30 minutes
```

On a **private** repo, Actions minutes count against your allowance (~1 min/run → ~1,440 min/month at 30-minute cadence, under the 2,000-minute free tier). On a **public** repo, Actions minutes are unlimited and free.

## How state works

`snapshot.json` (initialized as `{}`) stores `top_fingerprint` (open top-20, the general-update + pause trigger), `players` (per-roster-member records keyed by `NAME|division` with rank/rating/tier), a UTC `updated_at`, and — after a notification — `checks_paused_until`. After each run the workflow checks `git diff snapshot.json` and only commits + pushes when it changed, using the `github-actions[bot]` identity with `[skip ci]` so the push doesn't re-trigger the workflow. The legacy `{fingerprint,…}` schema auto-migrates: the first run under the new code establishes the new-schema baseline silently.

## Local testing

```bash
cd rankings_monitor
pip install -r requirements.txt
playwright install chromium
# Without SLACK_BOT_TOKEN, the script prints the channel message + intended DMs
# instead of sending — handy for previewing wording.
python monitor.py
```
