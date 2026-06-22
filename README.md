# NATS Rankings Monitor

This project watches the [NATS (USA Roundnet) Glicko-2 rankings](https://jmhyman.shinyapps.io/USAR-Rankings/) and posts a Slack notification whenever the top of the open division changes. A GitHub Actions workflow runs every 6 hours: it uses Playwright to scrape the JavaScript-rendered Shiny app, fingerprints the top 20 players' names and ratings, and — if the fingerprint differs from the last run — posts a static message to a Slack Incoming Webhook and commits an updated `snapshot.json`. If nothing changed or the page fails to load, it exits quietly and leaves the snapshot untouched. The only configuration you need is a single GitHub Secret.

## Deploy in under five minutes

1. **Fork this repo** (or copy the `rankings_monitor/` contents into your own repo — it must sit at the repository root, since `.github/workflows/` only runs from the root).
2. **Create a Slack Incoming Webhook** (see below) and copy its URL.
3. **Add the webhook as a secret:** in your repo go to **Settings → Secrets and variables → Actions → New repository secret**. Name it exactly `SLACK_WEBHOOK_URL` and paste the webhook URL as the value.
4. **Enable workflows:** open the **Actions** tab. If prompted with "Workflows aren't being run on this forked repository," click **I understand my workflows, go ahead and enable them**.
5. **Done.** The monitor now runs automatically every 6 hours.

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

The run appears in the list within a few seconds; click it to watch the logs.

## How state works

`snapshot.json` (initialized as `{}`) stores the latest fingerprint and a UTC timestamp. After each run the workflow checks `git diff snapshot.json` and only commits + pushes when it changed. Commits use the `github-actions[bot]` identity and include `[skip ci]` so the push doesn't re-trigger the workflow.

## Local testing

```bash
cd rankings_monitor
pip install -r requirements.txt
playwright install chromium
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."  # optional
python monitor.py
```
