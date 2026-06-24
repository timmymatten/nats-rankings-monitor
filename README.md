# NATS Rankings Monitor

This project watches the [NATS (USA Roundnet) Glicko-2 rankings](https://jmhyman.shinyapps.io/USAR-Rankings/) and posts a Slack notification whenever the top of the **open division** changes. A GitHub Actions workflow runs every 30 minutes: it uses Playwright to scrape the JavaScript-rendered Shiny app, fingerprints the top 20 open-division players' names and ratings, and — if the fingerprint differs from the last run — posts a static message to a Slack Incoming Webhook and commits an updated `snapshot.json`. If nothing changed or the page fails to load, it exits quietly and leaves the snapshot untouched. The only configuration you need is a single GitHub Secret.

> **Why "open division" specifically:** the Shiny page renders several tables (open lives in the `#player` container, women's in `#playerW`). The scraper scopes to `#player` so it always fingerprints the open division — the two divisions update at the same time, and reading the wrong one would fire a false "updated" alert.

## Pausing between tournaments

NATS tournaments only happen on **Saturdays**, so once a change is detected and the Slack notification fires, no new results can appear until the next Saturday's tournament. To avoid pointless scraping (which both burns Action runs and loads the source Shiny app), the monitor **pauses after a notification until the following Saturday**:

- When a change is detected, the script records a `checks_paused_until` timestamp in `snapshot.json` (next Saturday at 00:00).
- While inside that window, each run exits immediately — *before* launching the browser — so paused runs are near-instant and touch nothing.
- Once the window passes, normal 30-minute polling resumes and catches the new tournament's update whenever it lands.

The timezone that defines "Saturday" is the `TIMEZONE` constant in `monitor.py` (default `America/New_York`); change that one line to use a different region. The first-run baseline never pauses.

> **Note:** if a *correction* to the rankings is posted later in the same weekend (e.g. Sunday) after a notification already fired, it won't be picked up until the next Saturday — consistent with the assumption that meaningful changes only come from Saturday tournaments.

## Deploy in under five minutes

1. **Fork this repo** (or copy the `rankings_monitor/` contents into your own repo — it must sit at the repository root, since `.github/workflows/` only runs from the root).
2. **Create a Slack Incoming Webhook** (see below) and copy its URL.
3. **Add the webhook as a secret:** in your repo go to **Settings → Secrets and variables → Actions → New repository secret**. Name it exactly `SLACK_WEBHOOK_URL` and paste the webhook URL as the value.
4. **Enable workflows:** open the **Actions** tab. If prompted with "Workflows aren't being run on this forked repository," click **I understand my workflows, go ahead and enable them**.
5. **Done.** The monitor now runs automatically every 30 minutes.

The very first run establishes a baseline snapshot without sending a notification, so you won't get a spurious alert on day one. Every run after that notifies only when the rankings actually change.

## Creating the Slack Incoming Webhook

1. Go to <https://api.slack.com/apps> (**Settings → Your apps**).
2. Click **Create app → From scratch**, give it a name (e.g. "NATS Rankings"), pick your workspace, and create it.
3. In the app settings, open **Incoming Webhooks** and toggle **Activate Incoming Webhooks** on.
4. Click **Add New Webhook to Workspace**, choose the channel to post into, and **Allow**.
5. Copy the generated **Webhook URL** (looks like `https://hooks.slack.com/services/T000/B000/XXXX`). Use this as your `SLACK_WEBHOOK_URL` secret.

When the rankings change, the channel receives:

> 🏐 NATS Rankings updated!
> Check them out: https://www.usaroundnet.org/rankings

## Manually triggering a run

1. Open the **Actions** tab.
2. Select the **NATS Rankings Monitor** workflow in the left sidebar.
3. Click **Run workflow**, pick the branch (usually `main`), and click the green **Run workflow** button.
   - If checks are currently paused between tournaments (see above), toggle **Bypass the until-Saturday pause and check now** on to force a check anyway. Leave it off to respect the pause.

The run appears in the list within a few seconds; click it to watch the logs.

## Adjusting the schedule

The cadence is the `cron` line in `.github/workflows/monitor.yml`:

```yaml
- cron: "*/30 * * * *"   # every 30 minutes
```

Change it to suit (`0 * * * *` hourly, `0 */6 * * *` every 6 hours). Note that on a **private** repo, Actions minutes count against your monthly allowance (each run is billed as ~1 minute), so every 30 minutes is ~1,440 min/month — comfortably under the 2,000-minute free tier but worth keeping in mind. On a **public** repo, Actions minutes are unlimited and free.

## How state works

`snapshot.json` (initialized as `{}`) stores the latest fingerprint, a UTC timestamp, and — after a notification — a `checks_paused_until` timestamp (see [Pausing between tournaments](#pausing-between-tournaments)). After each run the workflow checks `git diff snapshot.json` and only commits + pushes when it changed. Commits use the `github-actions[bot]` identity and include `[skip ci]` so the push doesn't re-trigger the workflow.

## Local testing

```bash
cd rankings_monitor
pip install -r requirements.txt
playwright install chromium
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."  # optional
python monitor.py
```
