#!/usr/bin/env python3
"""List Slack workspace users as "Name -> U-ID" to help fill out roster.json.

Usage:
    SLACK_BOT_TOKEN=xoxb-... python tools/list_slack_users.py

Optionally narrow to the members of one channel (needs the bot to be a member of
that channel, plus a channels:read / groups:read scope):

    SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL_ID=C0123ABCD python tools/list_slack_users.py

Requires only the `requests` dependency (already used by the monitor) and the
`users:read` bot scope.
"""

import os
import sys

import requests

API = "https://slack.com/api"


def _call(method, token, params):
    resp = requests.get(
        f"{API}/{method}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data.get('error')}")
    return data


def all_users(token):
    """Yield user dicts across all pages of users.list."""
    cursor = ""
    while True:
        data = _call("users.list", token, {"limit": 200, "cursor": cursor})
        yield from data.get("members", [])
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break


def channel_member_ids(token, channel_id):
    """Return the set of user IDs in a channel, or None if scope is missing."""
    ids, cursor = set(), ""
    try:
        while True:
            data = _call(
                "conversations.members",
                token,
                {"channel": channel_id, "limit": 200, "cursor": cursor},
            )
            ids.update(data.get("members", []))
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        return ids
    except RuntimeError as exc:
        print(f"# Could not read channel members ({exc}); listing all users.\n",
              file=sys.stderr)
        return None


def main():
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("Set SLACK_BOT_TOKEN (xoxb-...) in the environment.", file=sys.stderr)
        return 1

    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    restrict = channel_member_ids(token, channel_id) if channel_id else None

    rows = []
    for u in all_users(token):
        if u.get("is_bot") or u.get("deleted") or u.get("id") == "USLACKBOT":
            continue
        if restrict is not None and u["id"] not in restrict:
            continue
        profile = u.get("profile", {})
        name = (
            profile.get("real_name")
            or profile.get("display_name")
            or u.get("name", "")
        )
        rows.append((name, u["id"]))

    rows.sort(key=lambda r: r[0].lower())
    scope = f"channel {channel_id}" if restrict is not None else "workspace"
    print(f"# {len(rows)} users in {scope}\n")
    for name, uid in rows:
        print(f"{name}  ->  {uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
