"""SQLite storage for notice titles."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    notice_id           TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    solicitation_number TEXT,
    notice_type         TEXT,
    agency              TEXT,
    agency_path         TEXT,
    naics_code          TEXT,
    classification_code TEXT,
    set_aside           TEXT,
    posted_date         TEXT,
    response_deadline   TEXT,
    archive_date        TEXT,
    active              INTEGER NOT NULL DEFAULT 0,
    ui_link             TEXT,
    award_number        TEXT,
    award_date          TEXT,
    award_amount        REAL,
    awardee             TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opp_active      ON opportunities(active);
CREATE INDEX IF NOT EXISTS idx_opp_posted      ON opportunities(posted_date);
CREATE INDEX IF NOT EXISTS idx_opp_agency      ON opportunities(agency);
CREATE INDEX IF NOT EXISTS idx_opp_deadline    ON opportunities(response_deadline);

-- Field-level history. the API overwrites in place, so changes are only visible
-- by diffing against what we stored. Once missed, unrecoverable.
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id   TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    detected_at TEXT NOT NULL,
    run_id      INTEGER,
    -- 'sync' = value differed; 'missing' = the API stopped returning it;
    -- 'archive_date' = inferred locally
    source      TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_notice   ON changes(notice_id);
CREATE INDEX IF NOT EXISTS idx_changes_detected ON changes(detected_at);
CREATE INDEX IF NOT EXISTS idx_changes_field    ON changes(field);

-- Rebuilt from profile.json on demand; separate so re-scoring can't touch
-- synced data.
CREATE TABLE IF NOT EXISTS scores (
    notice_id TEXT PRIMARY KEY,
    score     REAL NOT NULL,
    reasons   TEXT,
    scored_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_score ON scores(score DESC);

-- Cached; re-fetched only if the API edits the notice afterwards.
CREATE TABLE IF NOT EXISTS descriptions (
    notice_id       TEXT PRIMARY KEY,
    text            TEXT,
    chars           INTEGER,
    fetched_at      TEXT NOT NULL,
    posted_at_fetch TEXT
);

CREATE INDEX IF NOT EXISTS idx_desc_fetched ON descriptions(fetched_at);

-- Judged by hand in the dashboard: 'hide' for noise, 'keep' for the ones worth
-- reading. Both directions feed the Rocchio ranker, so a keep is worth as much
-- as a hide. One verdict per notice -- the two are opposites, not tags.
CREATE TABLE IF NOT EXISTS feedback (
    notice_id TEXT PRIMARY KEY,
    label     TEXT NOT NULL CHECK (label IN ('keep', 'hide')),
    -- What the ranker thought at the time. Without it there is no telling
    -- whether a hide corrected the ranking or just cleared expected noise.
    score_at  REAL,
    at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_label ON feedback(label);

-- Whatever has to outlive a process: currently just which ranker wrote
-- `scores`, so a sync re-runs the one you chose.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    posted_from  TEXT NOT NULL,
    posted_to    TEXT NOT NULL,
    seen         INTEGER NOT NULL DEFAULT 0,
    stored       INTEGER NOT NULL DEFAULT 0,
    api_requests INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

-- What the day's quota has actually been charged, by UTC day.
--
-- Counted rather than inferred. The obvious reading -- sum the sync runs, count
-- the descriptions saved -- misses every request the API charged for and then
-- refused: a 429 stores nothing, a failed fetch stores nothing, and a re-fetch
-- overwrites the row it already had. All three undercount, and the first one
-- undercounts at exactly the moment the number matters.
CREATE TABLE IF NOT EXISTS api_usage (
    day           TEXT PRIMARY KEY,
    sync_requests INTEGER NOT NULL DEFAULT 0,
    desc_requests INTEGER NOT NULL DEFAULT 0
);
"""

UPSERT = """
INSERT INTO opportunities (
    notice_id, title, solicitation_number, notice_type, agency, agency_path,
    naics_code, classification_code, set_aside, posted_date, response_deadline,
    archive_date, active, ui_link, award_number, award_date, award_amount, awardee,
    first_seen, last_seen
) VALUES (
    :notice_id, :title, :solicitation_number, :notice_type, :agency, :agency_path,
    :naics_code, :classification_code, :set_aside, :posted_date, :response_deadline,
    :archive_date, :active, :ui_link, :award_number, :award_date, :award_amount, :awardee,
    :now, :now
)
ON CONFLICT(notice_id) DO UPDATE SET
    title               = excluded.title,
    solicitation_number = excluded.solicitation_number,
    notice_type         = excluded.notice_type,
    agency              = excluded.agency,
    agency_path         = excluded.agency_path,
    naics_code          = excluded.naics_code,
    classification_code = excluded.classification_code,
    set_aside           = excluded.set_aside,
    posted_date         = excluded.posted_date,
    response_deadline   = excluded.response_deadline,
    archive_date        = excluded.archive_date,
    active              = excluded.active,
    ui_link             = excluded.ui_link,
    award_number        = excluded.award_number,
    award_date          = excluded.award_date,
    award_amount        = excluded.award_amount,
    awardee             = excluded.awardee,
    last_seen           = excluded.last_seen
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)")}
    added = [
        ("award_number", "TEXT"),
        ("award_date", "TEXT"),
        ("award_amount", "REAL"),
        ("awardee", "TEXT"),
        ("classification_code", "TEXT"),
    ]
    with conn:
        for column, decl in added:
            if column not in existing:
                conn.execute(f"ALTER TABLE opportunities ADD COLUMN {column} {decl}")

        have = {row[1] for row in conn.execute("PRAGMA table_info(changes)")}
        if "source" not in have:
            conn.execute("ALTER TABLE changes ADD COLUMN source TEXT")

        # The theme moved to the browser; nothing else used this table.
        conn.execute("DROP TABLE IF EXISTS settings")

        # Seed api_usage from what the old derivation could see, once. It
        # undercounts refusals -- that is why it is being replaced -- but it is
        # the only record of days already spent, and starting today's row at zero
        # would report a fresh budget to someone who has none left.
        empty = not conn.execute("SELECT 1 FROM api_usage LIMIT 1").fetchone()
        if empty:
            conn.execute(
                """INSERT INTO api_usage (day, sync_requests, desc_requests)
                   SELECT day, SUM(s), SUM(d) FROM (
                       SELECT substr(started_at, 1, 10) AS day,
                              COALESCE(api_requests, 0) AS s, 0 AS d
                       FROM sync_runs
                       UNION ALL
                       SELECT substr(fetched_at, 1, 10), 0, 1 FROM descriptions
                   ) GROUP BY day"""
            )
            # A run that ended 'rate limited' sent a request the old counter
            # never saw -- refusals stored nothing. One per such run is the
            # least it can have been, and it is the difference between a
            # spent day reading 999 and reading 1000.
            conn.execute(
                """UPDATE api_usage SET sync_requests = sync_requests + COALESCE((
                       SELECT COUNT(*) FROM sync_runs
                       WHERE substr(started_at, 1, 10) = api_usage.day
                         AND error = 'rate limited'
                   ), 0)"""
            )

        # `hidden` became one label in `feedback`, alongside 'keep'.
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hidden'"
        ).fetchone()
        if legacy:
            conn.execute(
                """INSERT OR IGNORE INTO feedback (notice_id, label, score_at, at)
                   SELECT notice_id, 'hide', NULL, hidden_at FROM hidden"""
            )
            conn.execute("DROP TABLE hidden")


def blank_to_none(value: Any) -> Any:
    """Normalise the API's empty strings to NULL.

    the API returns "" as often as it omits a field. Stored verbatim, an empty
    string is not NULL: it satisfies `IS NOT NULL`, and it sorts before every
    real date, so rows with no deadline masquerade as ones with a very old one.
    """
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def to_row(record: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one API record into a database row, or None if unusable."""
    notice_id = (record.get("noticeId") or "").strip()
    title = (record.get("title") or "").strip()
    if not notice_id or not title:
        return None

    agency_path = (record.get("fullParentPathName") or "").strip()

    award = record.get("award") or {}
    awardee = (award.get("awardee") or {}).get("name")
    try:
        amount = float(award["amount"]) if award.get("amount") not in (None, "") else None
    except (TypeError, ValueError):
        amount = None

    row = {
        "notice_id": notice_id,
        "title": title,
        "solicitation_number": record.get("solicitationNumber"),
        "notice_type": record.get("type"),
        "agency": agency_path.split(".")[0] if agency_path else None,
        "agency_path": agency_path or None,
        "naics_code": record.get("naicsCode"),
        # PSC: what is being bought. More reliable than NAICS.
        "classification_code": record.get("classificationCode"),
        "set_aside": record.get("typeOfSetAsideDescription"),
        "posted_date": record.get("postedDate"),
        "response_deadline": record.get("responseDeadLine"),
        "archive_date": record.get("archiveDate"),
        "active": 1 if str(record.get("active", "")).strip().lower() == "yes" else 0,
        "ui_link": record.get("uiLink"),
        "award_number": award.get("number"),
        "award_date": award.get("date"),
        "award_amount": amount,
        "awardee": awardee,
    }
    return {k: blank_to_none(v) for k, v in row.items()}


# Columns worth keeping history for.
TRACKED_FIELDS = (
    "title",
    "solicitation_number",
    "notice_type",
    "agency",
    "naics_code",
    "classification_code",
    "set_aside",
    "posted_date",
    "response_deadline",
    "archive_date",
    "active",
    "award_number",
    "award_date",
    "award_amount",
    "awardee",
)


def diff_rows(old: sqlite3.Row, new: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Fields that differ between a stored row and an incoming one."""
    changed = []
    for field in TRACKED_FIELDS:
        before, after = old[field], new.get(field)
        if before is None and after is None:
            continue
        # Compare numerics numerically, or 90000 vs 90000.0 reads as a change.
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            if float(before) == float(after):
                continue
        elif before == after:
            continue
        changed.append((field, before, after))
    return changed


def upsert_many(
    conn: sqlite3.Connection,
    rows: Iterable[dict[str, Any]],
    *,
    run_id: int | None = None,
    track_changes: bool = True,
) -> tuple[int, int]:
    """Insert or update notices, recording what changed. Returns (stored, changes)."""
    now = utcnow()
    batch = [{**row, "now": now} for row in rows]
    if not batch:
        return 0, 0

    changes: list[tuple] = []
    if track_changes:
        ids = [row["notice_id"] for row in batch]
        placeholders = ",".join("?" * len(ids))
        stored = {
            row["notice_id"]: row
            for row in conn.execute(
                f"SELECT * FROM opportunities WHERE notice_id IN ({placeholders})", ids
            )
        }
        for row in batch:
            previous = stored.get(row["notice_id"])
            if previous is None:
                continue  # brand new notice -- not a change
            for field, before, after in diff_rows(previous, row):
                changes.append(
                    (row["notice_id"], field,
                     None if before is None else str(before),
                     None if after is None else str(after),
                     now, run_id)
                )

    with conn:
        conn.executemany(UPSERT, batch)
        if changes:
            conn.executemany(
                """INSERT INTO changes
                   (notice_id, field, old_value, new_value, detected_at, run_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, 'sync')""",
                changes,
            )
    return len(batch), len(changes)


def is_expired(deadline: str | None, today: str) -> bool:
    """True once the response deadline is strictly in the past.

    A deadline falling today still counts as open. Notices with no deadline at
    all (award notices, most special notices) are never "expired" -- there is
    nothing to have passed.
    """
    return bool(deadline) and deadline[:10] < today


def deactivate_missing(
    conn: sqlite3.Connection,
    posted_from: str,
    posted_to: str,
    run_started: str,
    run_id: int | None = None,
) -> int:
    """Flag stored notices that the API no longer returns as inactive.

    Once a notice archives it drops out of search results entirely, so its row
    would otherwise keep claiming active=1 forever. Anything inside the window we
    just swept that was not touched by this run has gone away upstream. Scoped to
    the synced window so a narrow `--days 7` run cannot deactivate older rows.
    """
    gone = [
        row[0]
        for row in conn.execute(
            """SELECT notice_id FROM opportunities
               WHERE active = 1
                 AND posted_date >= ? AND posted_date <= ?
                 AND last_seen < ?""",
            (posted_from, posted_to, run_started),
        )
    ]
    if not gone:
        return 0

    now = utcnow()
    with conn:
        conn.executemany(
            "UPDATE opportunities SET active = 0 WHERE notice_id = ?",
            [(nid,) for nid in gone],
        )
        # Archival is a change like any other; record it in the same history.
        conn.executemany(
            """INSERT INTO changes
               (notice_id, field, old_value, new_value, detected_at, run_id, source)
               VALUES (?, 'active', '1', '0', ?, ?, 'missing')""",
            [(nid, now, run_id) for nid in gone],
        )
    return len(gone)


def sweep_archived(conn: sqlite3.Connection, today: str, run_id: int | None = None) -> int:
    """Retire notices whose own archive date has passed, without an API call.

    A narrow sync only sees absence inside its own window, so notices posted
    outside it would keep claiming active=1 forever. the API states an archiveDate
    per notice, so a passed one is good evidence the notice is gone. This is an
    INFERENCE, not an observation -- agencies do move archive dates -- so it is
    recorded with source='archive_date', and a wider sync will correct any drift.
    """
    stale = [
        row[0]
        for row in conn.execute(
            """SELECT notice_id FROM opportunities
               WHERE active = 1 AND archive_date IS NOT NULL AND archive_date < ?""",
            (today,),
        )
    ]
    if not stale:
        return 0

    now = utcnow()
    with conn:
        conn.executemany(
            "UPDATE opportunities SET active = 0 WHERE notice_id = ?",
            [(nid,) for nid in stale],
        )
        conn.executemany(
            """INSERT INTO changes
               (notice_id, field, old_value, new_value, detected_at, run_id, source)
               VALUES (?, 'active', '1', '0', ?, ?, 'archive_date')""",
            [(nid, now, run_id) for nid in stale],
        )
    return len(stale)


# No foreign keys here, so a delete must name every table or leave orphans.
CHILD_TABLES = ("scores", "descriptions", "changes", "feedback")

# Years of notices to keep.
RETENTION_YEARS = 3


def retention_cutoff(today: str, years: int = RETENTION_YEARS) -> str:
    """The oldest posted_date worth keeping, `years` back from `today`."""
    day = date.fromisoformat(today)
    try:
        return day.replace(year=day.year - years).isoformat()
    except ValueError:
        # 29 February: the same date doesn't exist in a non-leap year.
        return day.replace(year=day.year - years, day=28).isoformat()


def _delete_notices(conn: sqlite3.Connection, where: str, params: tuple) -> dict[str, int]:
    """Delete notices and their child rows. Children first."""
    counts: dict[str, int] = {}
    with conn:
        for table in CHILD_TABLES:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE notice_id IN "
                f"(SELECT notice_id FROM opportunities WHERE {where})",
                params,
            )
            counts[table] = cursor.rowcount
        cursor = conn.execute(f"DELETE FROM opportunities WHERE {where}", params)
        counts["opportunities"] = cursor.rowcount
    return counts


def prune_expired(conn: sqlite3.Connection, today: str) -> dict[str, int]:
    """Delete stored notices whose response deadline has passed."""
    return _delete_notices(
        conn,
        "response_deadline IS NOT NULL AND substr(response_deadline, 1, 10) < ?",
        (today,),
    )


def prune_older_than(
    conn: sqlite3.Connection,
    cutoff: str,
    today: str | None = None,
    keep_open: bool = True,
) -> dict[str, int]:
    """Delete notices posted before `cutoff`.

    Rows with no posted_date are kept. `keep_open` spares anything whose
    deadline is still ahead, however old.
    """
    where = "posted_date IS NOT NULL AND posted_date < ?"
    params: tuple = (cutoff,)
    if keep_open:
        if today is None:
            raise ValueError("keep_open needs today's date to judge a deadline")
        where += " AND (response_deadline IS NULL OR substr(response_deadline, 1, 10) < ?)"
        params += (today,)
    return _delete_notices(conn, where, params)


# -- feedback ---------------------------------------------------------------

LABELS = ("keep", "hide")


def labelled_ids(conn: sqlite3.Connection, label: str) -> list[str]:
    """Notices carrying one verdict, oldest first."""
    return [
        r[0] for r in conn.execute(
            "SELECT notice_id FROM feedback WHERE label = ? ORDER BY at", (label,)
        )
    ]


def hidden_ids(conn: sqlite3.Connection) -> list[str]:
    return labelled_ids(conn, "hide")


def kept_ids(conn: sqlite3.Connection) -> list[str]:
    return labelled_ids(conn, "keep")


def feedback_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = dict.fromkeys(LABELS, 0)
    for label, n in conn.execute("SELECT label, COUNT(*) FROM feedback GROUP BY label"):
        counts[label] = n
    return counts


def set_feedback(
    conn: sqlite3.Connection, notice_ids: Iterable[str], label: str | None
) -> int:
    """Label notices 'keep' or 'hide', or clear the verdict with None.

    The two labels overwrite each other: a notice you star was, by definition,
    not one you wanted hidden. Only ids we store are accepted, and the ranker's
    current opinion is recorded alongside so a later pass can tell which
    verdicts it had already got right.
    """
    ids = [n for n in notice_ids if n]
    if not ids:
        return 0
    if label is not None and label not in LABELS:
        raise ValueError(f"label must be one of {LABELS} or None, not {label!r}")

    marks = ",".join("?" * len(ids))
    with conn:
        if label is None:
            cursor = conn.execute(
                f"DELETE FROM feedback WHERE notice_id IN ({marks})", ids
            )
        else:
            cursor = conn.execute(
                f"""INSERT INTO feedback (notice_id, label, score_at, at)
                    SELECT o.notice_id, ?, s.score, ?
                    FROM opportunities o LEFT JOIN scores s USING (notice_id)
                    WHERE o.notice_id IN ({marks})
                    ON CONFLICT(notice_id) DO UPDATE SET
                        label = excluded.label,
                        score_at = excluded.score_at,
                        at = excluded.at""",
                (label, utcnow(), *ids),
            )
    return cursor.rowcount


def set_hidden(conn: sqlite3.Connection, notice_ids: Iterable[str], hidden: bool) -> int:
    """Hide or unhide -- the older two-state call, kept for the legacy import."""
    return set_feedback(conn, notice_ids, "hide" if hidden else None)


# -- meta -------------------------------------------------------------------

# Which command last wrote `scores`. A sync re-runs that one, or switching
# rankers would silently revert on the next sync.
DEFAULT_RANKER = "rules"


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def active_ranker(conn: sqlite3.Connection) -> str:
    return get_meta(conn, "ranker", DEFAULT_RANKER) or DEFAULT_RANKER


def write_scores(
    conn: sqlite3.Connection, rows: Iterable[tuple[str, float, str]], ranker: str
) -> int:
    """Replace every score in one transaction, and record who wrote them."""
    now = utcnow()
    batch = [(nid, score, reasons, now) for nid, score, reasons in rows]
    with conn:
        conn.execute("DELETE FROM scores")
        conn.executemany(
            "INSERT INTO scores (notice_id, score, reasons, scored_at) VALUES (?, ?, ?, ?)",
            batch,
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('ranker', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (ranker,),
        )
    return len(batch)


def orphaned_child_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows a delete failed to cascade to."""
    return {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE notice_id NOT IN "
            "(SELECT notice_id FROM opportunities)"
        ).fetchone()[0]
        for table in CHILD_TABLES
    }


# Title language that means the notice is buying a thing, not commissioning
# work. Cheapest possible filter -- applied before spending any request.
COMMODITY_TERMS = (
    "construction", "renovation", "roofing", "roof ", "paving", "hvac", "plumbing",
    "janitorial", "custodial", "landscaping", "lawn", "food service", "catering",
    "laundry", "uniform", "vehicle", "ammunition", "medical supplies", "dental",
    "pharmaceutical", "guard services", "fuel", "furniture", "valve", "connector",
    "gasket", "bearing", "hose", "pump", "filter", "lumber", "repair of",
    "maintenance of", "generator", "boiler", "switchgear", "ventilator",
    "elevator", "pest control", "snow removal", "waste removal", "trash",
    "recycling", "shredding", "flooring", "painting", "window replacement",
    "chiller", "compressor", "forklift", "tractor", "mower", "duct", "piping",
    "sprinkler", "fire alarm", "grease", "oil change", "tire",
)

# A purely numeric PSC is a Federal Supply Class -- a physical item being bought,
# not work being commissioned. Letter-led codes are services and stay in play,
# since useful work hides under odd ones (an AI notice under a medical code).
SUPPLY_PSC = "classification_code GLOB '[0-9]*' AND classification_code NOT GLOB '7[A-Z]*'"

BIDDABLE_TYPES = (
    "Solicitation",
    "Combined Synopsis/Solicitation",
    "Sources Sought",
    "Presolicitation",
)


# Priority bands. Every notice is fetched eventually, so these decide the order
# and never the membership. Each gap is wider than any within-band total can
# reach, so a band always outranks the one below it however the details fall.
BAND_OPEN_BIDDABLE = 10_000
BAND_OPEN = 6_000
BAND_CLOSED = 2_000
BAND_ARCHIVED = 0

# Within a band, fit leads: it is the profile's own verdict on the notice and
# already folds in NAICS, PSC, keywords, set-aside and notice type.
FIT_WEIGHT = 4                  # fit is 0-100, so this contributes 0-400
BOILERPLATE_PENALTY = 150       # commodity buys sink inside their own band


def _day_diff(start: str | None, end: str | None) -> int | None:
    """Whole days from `start` to `end`, or None if either date is unusable."""
    try:
        return (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
    except (ValueError, TypeError):
        return None


def _urgency(deadline: str | None, today: str) -> int:
    """0-100, highest for whatever closes soonest.

    A missing deadline is not a closed notice -- Sources Sought in particular
    often post without one -- so it sits mid-pack rather than last.
    """
    if not deadline:
        return 20
    days = _day_diff(today, deadline)
    if days is None or days < 0:
        return 0
    return max(0, 100 - days)


def _recency(posted: str | None, today: str) -> int:
    """0-60, highest for whatever was posted most recently."""
    age = _day_diff(posted, today)
    if age is None:
        return 0
    return max(0, 60 - max(0, age) // 3)


def description_queue(
    conn: sqlite3.Connection, today: str, limit: int, profile: dict | None = None
) -> list[sqlite3.Row]:
    """Every notice still needing text, the ones worth reading first, first.

    Nothing is excluded. The API keeps serving a description long after a notice
    closes or archives, so each one is fetched eventually. What used to be the
    entry requirement -- still open, biddable, not a commodity buy -- is the
    ordering instead: the notices worth acting on are read first and the rest
    are picked up with whatever quota is left over.

    The order is a band plus a score. The band answers "can this still be bid?"
    -- open and biddable, open, closed, archived -- and the score inside it is
    the profile's own fit verdict, lifted by how soon the notice closes and how
    recently it was posted. Commodity and FSC-tagged titles are demoted rather
    than dropped, so they land at the bottom of their band instead of nowhere.

    Fit is read from the `scores` table, which covers every notice that a `score`
    run has seen. One synced minutes ago has no row yet, so its title is scored
    on the spot rather than sinking to the bottom for the sin of being new.
    """
    commodity = " OR ".join(["lower(o.title) LIKE ?"] * len(COMMODITY_TERMS))
    sql = f"""
        SELECT o.notice_id, o.title, o.posted_date, o.response_deadline,
               o.notice_type, o.classification_code, o.naics_code, o.set_aside,
               o.active, s.score AS fit,
               -- FSC-tagged titles at any prefix length: "Y--", "59--", "J059--"
               ({commodity}
                OR o.title GLOB '[0-9A-Z]--*'
                OR o.title GLOB '[0-9A-Z][0-9A-Z]--*'
                OR o.title GLOB '[0-9A-Z][0-9A-Z][0-9A-Z]--*'
                OR o.title GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]--*'
                OR (o.{SUPPLY_PSC})) AS boilerplate
        FROM opportunities o
        LEFT JOIN descriptions d USING (notice_id)
        LEFT JOIN scores s USING (notice_id)
        -- never fetched, or fetched before the API last edited the notice
        WHERE (d.notice_id IS NULL OR d.posted_at_fetch < o.posted_date)
    """
    rows = conn.execute(sql, [f"%{t}%" for t in COMMODITY_TERMS]).fetchall()

    scorer = None
    ranked: list[tuple[int, int, sqlite3.Row]] = []
    for position, row in enumerate(rows):
        fit = row["fit"]
        if fit is None:
            if scorer is None:
                from .scoring import Scorer, load_profile
                scorer = Scorer(profile or load_profile())
            fit = scorer.score(row)[0]

        deadline = row["response_deadline"]
        still_open = not deadline or deadline[:10] >= today
        if not row["active"]:
            band = BAND_ARCHIVED
        elif still_open and row["notice_type"] in BIDDABLE_TYPES:
            band = BAND_OPEN_BIDDABLE
        elif still_open:
            band = BAND_OPEN
        else:
            band = BAND_CLOSED

        score = (round(fit * FIT_WEIGHT)
                 + _urgency(deadline, today)
                 + _recency(row["posted_date"], today))
        if row["boilerplate"]:
            score -= BOILERPLATE_PENALTY
        # `position` keeps the sort stable and stops it comparing Rows on a tie.
        ranked.append((-(band + score), position, row))

    ranked.sort()
    return [row for _, _, row in ranked[:limit]]


def save_description(conn: sqlite3.Connection, notice_id: str, text: str, posted: str) -> None:
    with conn:
        conn.execute(
            """INSERT INTO descriptions (notice_id, text, chars, fetched_at, posted_at_fetch)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(notice_id) DO UPDATE SET
                   text = excluded.text, chars = excluded.chars,
                   fetched_at = excluded.fetched_at,
                   posted_at_fetch = excluded.posted_at_fetch""",
            (notice_id, text, len(text), utcnow(), posted),
        )


def record_requests(conn: sqlite3.Connection, kind: str, count: int) -> None:
    """Charge `count` requests to today's budget.

    Additive, so a command can report what it spent without knowing or caring
    what ran before it. `kind` is 'sync' or 'desc' -- the two draw on one
    allowance, and the split exists only so the page can say where it went.
    """
    if count <= 0:
        return
    column = "sync_requests" if kind == "sync" else "desc_requests"
    utc_today = datetime.now(timezone.utc).date().isoformat()
    with conn:
        conn.execute(
            f"""INSERT INTO api_usage (day, {column}) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET {column} = {column} + excluded.{column}""",
            (utc_today, int(count)),
        )


def requests_used_today(conn: sqlite3.Connection) -> tuple[int, int]:
    """(sync requests, description requests) spent so far this UTC day.

    Both count against the same quota, so the description budget has to know
    what the sync already spent or the pair can overshoot together.

    Read from `api_usage`, which records what was sent. Counting saved rows
    instead would quietly omit every request the API charged for and refused --
    and the first of those is the 429 that says the day is over.
    """
    utc_today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT sync_requests, desc_requests FROM api_usage WHERE day = ?", (utc_today,)
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def fetched_today(conn: sqlite3.Connection) -> int:
    """Descriptions fetched so far in the current UTC day.

    Must use UTC, not the local date: fetched_at is written in UTC, so comparing
    it to a local date makes the counter reset mid-evening for anyone west of
    Greenwich -- and the budget stops binding exactly when it matters.
    """
    utc_today = datetime.now(timezone.utc).date().isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM descriptions WHERE substr(fetched_at, 1, 10) = ?", (utc_today,)
    ).fetchone()[0]


def last_synced_to(conn: sqlite3.Connection) -> str | None:
    """End of the window covered by the most recent successful sync."""
    row = conn.execute(
        """SELECT posted_to FROM sync_runs
           WHERE error IS NULL AND finished_at IS NOT NULL
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    return row[0] if row else None


def start_run(conn: sqlite3.Connection, posted_from: str, posted_to: str) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO sync_runs (started_at, posted_from, posted_to) VALUES (?, ?, ?)",
            (utcnow(), posted_from, posted_to),
        )
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    seen: int,
    stored: int,
    api_requests: int,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """UPDATE sync_runs
               SET finished_at = ?, seen = ?, stored = ?, api_requests = ?, error = ?
               WHERE id = ?""",
            (utcnow(), seen, stored, api_requests, error, run_id),
        )
