"""Build a self-contained HTML dashboard from the stored notice titles.

    python lib/dashboard.py             # writes dashboard.html and opens it
    python lib/dashboard.py --no-open

Normally you don't run this yourself -- a sync rebuilds the page at the end.

Re-run it after each `main.py sync` to refresh the page.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import webbrowser
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Run as a script, so add the root -- see lib/serve.py.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.store import (                               # noqa: E402  (needs the path above)
    connect,
    hidden_ids,
    kept_ids,
    requests_used_today,
)
from main import load_dotenv                          # noqa: E402  (same reason)

TEMPLATE = HERE / "dashboard_template.html"

# serve.py rewrites this per request.
FEEDBACK_SLOT = "var SAVED_HIDDEN = null, SAVED_KEPT = null, SAVED_REQUESTS = null;"

# Per notice, how much edit history to carry into the page.
MAX_CHANGES_PER_NOTICE = 8

QUERY = """
SELECT o.title, o.agency, o.notice_type, o.posted_date, o.response_deadline,
       o.notice_id, o.active, o.award_amount, o.awardee,
       COALESCE(s.score, 0) AS score, s.reasons,
       substr(d.text, 1, 400) AS snippet, d.chars, o.naics_code,
       -- Any award field, not just the amount.
       (o.award_number IS NOT NULL OR o.award_date IS NOT NULL
        OR o.award_amount IS NOT NULL OR o.awardee IS NOT NULL) AS awarded
FROM opportunities o
LEFT JOIN scores s USING (notice_id)
LEFT JOIN descriptions d USING (notice_id)
"""

# Below this, skip the score breakdown.
REASONS_FLOOR = 20


def build_payload(conn: sqlite3.Connection) -> dict:
    agencies: dict[str, int] = {}
    types: dict[str, int] = {}
    naics: dict[str, int] = {}
    rows: list[list] = []
    lo, hi = "9999-99-99", "0000-00-00"

    why: dict[str, list] = {}
    snippets: dict[str, list] = {}
    for (title, agency, notice_type, posted, deadline, notice_id,
         active, award_amount, awardee, score, reasons,
         snippet, chars, naics_code, awarded) in conn.execute(QUERY):
        agency = agency or "(unknown)"
        notice_type = notice_type or "(unknown)"
        # Plenty of notices carry no code; "(none)" sorts ahead of the digits.
        naics_code = (naics_code or "").strip() or "(none)"
        posted = (posted or "")[:10]
        if not posted:
            continue
        lo, hi = min(lo, posted), max(hi, posted)
        rows.append([
            title,
            agencies.setdefault(agency, len(agencies)),
            types.setdefault(notice_type, len(types)),
            posted,
            (deadline or "")[:10] or None,
            notice_id,
            1 if active else 0,
            round(award_amount) if award_amount else None,
            awardee or None,
            round(score or 0),
            1 if snippet else 0,
            1 if awarded else 0,
            naics.setdefault(naics_code, len(naics)),
        ])
        if score and score >= REASONS_FLOOR and reasons:
            why[notice_id] = json.loads(reasons)
        if snippet:
            snippets[notice_id] = [snippet, chars or 0]

    # Remap the lookup indices so the dropdowns read alphabetically.
    def sorted_lookup(seen: dict[str, int]) -> tuple[list[str], dict[int, int]]:
        names = sorted(seen)
        remap = {seen[name]: i for i, name in enumerate(names)}
        return names, remap

    agency_names, agency_map = sorted_lookup(agencies)
    type_names, type_map = sorted_lookup(types)
    naics_names, naics_map = sorted_lookup(naics)
    for row in rows:
        row[1] = agency_map[row[1]]
        row[2] = type_map[row[2]]
        row[12] = naics_map[row[12]]

    # Its own map, not columns: few notices change.
    changes: dict[str, list] = {}
    for notice_id, field, old, new, when in conn.execute(
        """SELECT notice_id, field, old_value, new_value, detected_at
           FROM changes ORDER BY id DESC"""
    ):
        entries = changes.setdefault(notice_id, [])
        if len(entries) < MAX_CHANGES_PER_NOTICE:
            entries.append([field, old, new, (when or "")[:10]])

    return {
        "generated": date.today().isoformat(),
        "today": max(hi, date.today().isoformat()),
        "range": [lo, hi],
        # Where a row's title links to. Templated with {id} and kept in .env so
        # the page source carries no vendor detail; blank simply drops the link.
        "noticeUrl": os.environ.get("NOTICE_URL", "").strip(),
        # A snapshot for the counter to show before the first heartbeat answers,
        # and the only figure there is when the page is opened off disk.
        "requests": dict(zip(("sync", "desc"), requests_used_today(conn))),
        "agencies": agency_names,
        "types": type_names,
        "naics": naics_names,
        "rows": rows,
        "changes": changes,
        "why": why,
        "snippets": snippets,
        # For file:// use; serve.py injects the live lists.
        "hidden": hidden_ids(conn),
        "kept": kept_ids(conn),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "notices.db"))
    parser.add_argument("--out", default=str(ROOT / "data" / "dashboard.html"))
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    # serve.py runs this directly, so it can't rely on the CLI having loaded .env.
    load_dotenv(ROOT / ".env")

    # store.connect applies the schema.
    conn = connect(args.db)
    payload = build_payload(conn)
    conn.close()

    if not payload["rows"]:
        print("No active notices stored yet -- run `python main.py sync` first.")
        return 1

    # Only "</script" can break out of the JSON block.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", blob)

    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    size = out.stat().st_size / 1e6
    print(f"{len(payload['rows']):,} notices -> {out.resolve()} ({size:.1f} MB)")

    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
