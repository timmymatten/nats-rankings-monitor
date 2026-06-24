#!/usr/bin/env python3
"""Validate roster.json against the live rankings before deploying.

For every roster entry it checks whether the name resolves (exact, after the same
normalization monitor.py uses) in each listed division. For misses it suggests the
closest names on the page (so you can fix a nickname/accent/typo), and it flags names
that actually live in a division you did NOT list.

Runtime matching in monitor.py is intentionally exact — never fuzzy — so a wrong
guess can't DM the wrong person. This tool is the place to resolve mismatches.

Usage:
    python tools/validate_roster.py            # validates ../roster.json
    python tools/validate_roster.py path.json
"""

import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402


def main():
    roster_path = Path(sys.argv[1]) if len(sys.argv) > 1 else monitor.ROSTER_PATH
    raw = json.loads(Path(roster_path).read_text(encoding="utf-8") or "[]")
    if not raw:
        print(f"{roster_path} is empty — nothing to validate.")
        return 0

    print("Scraping live rankings (both divisions)…")
    scraped = monitor.scrape()
    by_div_names = {d: list(players) for d, players in scraped.items()}

    ok, problems = 0, 0
    for entry in raw:
        name = entry.get("name", "")
        norm = monitor.normalize_name(name)
        divisions = entry.get("divisions", [])
        dm = "DM" if entry.get("slack_id") else "shoutout-only"

        for div in divisions:
            if div not in monitor.DIVISIONS:
                print(f"✗ {name}: unknown division '{div}'")
                problems += 1
                continue
            if norm in scraped.get(div, {}):
                rec = scraped[div][norm]
                print(f"✓ {name} [{div}] → rank {rec['rank']}, {rec['rating']}, "
                      f"{rec['tier']} ({dm})")
                ok += 1
            else:
                problems += 1
                suggestions = difflib.get_close_matches(
                    norm, by_div_names.get(div, []), n=3, cutoff=0.6
                )
                print(f"✗ {name} [{div}] NOT FOUND."
                      + (f" Did you mean: {', '.join(suggestions)}?" if suggestions
                         else " No close matches."))
        # flag divisions where the name DOES appear but wasn't listed
        for div in monitor.DIVISIONS:
            if div not in divisions and norm in scraped.get(div, {}):
                print(f"  ℹ {name} also appears in '{div}' (not listed in divisions).")

    print(f"\n{ok} record(s) matched, {problems} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
