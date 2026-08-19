"""Fetch active notice titles and store them in SQLite.

Usage:
    python main.py sync                 # pull the last 365 days of active notices
    python main.py sync --days 30       # narrower window (fewer API calls)
    python main.py score                # rank against profile.json
    python main.py rocchio              # rank against what you starred and hid
    python main.py titles --search "cyber"
    python main.py stats
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
from datetime import date, timedelta
from pathlib import Path

from lib.rocchio import (
    ALPHA as ROCCHIO_ALPHA,
    BETA as ROCCHIO_BETA,
    GAMMA as ROCCHIO_GAMMA,
    MIN_DF as ROCCHIO_MIN_DF,
    PRIOR as ROCCHIO_PRIOR,
    SLOPE as ROCCHIO_SLOPE,
    TITLE_WEIGHT as ROCCHIO_TITLE_WEIGHT,
)
from lib.api_client import DAILY_QUOTA, MAX_LIMIT, RateLimited, ApiError, ApiClient
from lib.store import (
    connect,
    deactivate_missing,
    description_queue,
    feedback_counts,
    finish_run,
    hidden_ids,
    is_expired,
    kept_ids,
    last_synced_to,
    orphaned_child_rows,
    prune_expired,
    record_requests,
    prune_older_than,
    requests_used_today,
    retention_cutoff,
    RETENTION_YEARS,
    save_description,
    set_feedback,
    start_run,
    sweep_archived,
    to_row,
    upsert_many,
    utcnow,
    write_scores,
)

DEFAULT_DB = Path(__file__).parent / "data" / "notices.db"
BATCH_SIZE = 500

# Conventional "died on a signal" status, so callers can tell a stop from a
# fault -- serve.py keys the Stop button's reporting off it.
INTERRUPTED_EXIT = 130

log = logging.getLogger("notices")


def load_dotenv(path: Path) -> None:
    """Minimal .env reader; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def resolve_api_key(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("API_KEY", "")
    if not key:
        raise ApiError(
            "No API key found.\n"
            "  1. Get a key from the API provider\n"
            "  2. Put it in a .env file next to this script:  API_KEY=your_key_here\n"
            "     ...or pass it with --api-key."
        )
    return key


# -- commands ---------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    api_key = resolve_api_key(args)

    conn = connect(args.db)

    # A refused request is a spent request: the quota is charged for sending it,
    # not for liking the answer. So a sync started with the day's allowance
    # already gone buys one 429 and charges the counter for it -- which is how a
    # 1,000-request day reads 1,001, then 1,002, one launch at a time. Check the
    # ledger before opening a connection. `--cap 0` restores the old behaviour of
    # asking the API and letting it say no.
    if args.cap > 0:
        spent = sum(requests_used_today(conn))
        if spent >= args.cap:
            print(f"Daily API quota used up: {spent} of {args.cap} requests spent "
                  "today. It resets at midnight UTC. Nothing fetched, and nothing "
                  "charged for asking.")
            conn.close()
            return 0

    # A narrow window still catches amendments: they re-date the notice.
    days = args.days if args.days is not None else (2 if args.daily else 365)

    posted_to = date.fromisoformat(args.to) if args.to else date.today()
    explicit_from = getattr(args, "from")

    if explicit_from:
        posted_from = date.fromisoformat(explicit_from)
    elif args.since_last:
        # Minus a day of overlap, so nothing on the boundary slips through.
        previous = last_synced_to(conn)
        posted_from = (
            date.fromisoformat(previous) - timedelta(days=1)
            if previous
            else posted_to - timedelta(days=days)
        )
        if previous:
            log.info("Resuming from last sync (%s) with a 1-day buffer", previous)
        else:
            log.info("No previous sync recorded; falling back to %d days", days)
        posted_from = min(posted_from, posted_to)
    else:
        posted_from = posted_to - timedelta(days=days)

    client = ApiClient(api_key, sleep_between=args.sleep, stop_on_rate_limit=True)
    run_id = start_run(conn, posted_from.isoformat(), posted_to.isoformat())

    seen = stored = skipped = expired = changed = 0
    batch: list[dict] = []
    error: str | None = None
    today = date.today().isoformat()
    run_started = utcnow()

    log.info("Syncing notices posted %s .. %s", posted_from, posted_to)
    try:
        records = client.iter_opportunities(
            posted_from,
            posted_to,
            limit=args.limit,
            notice_types=args.ptype or None,
        )
        for record in records:
            seen += 1
            row = to_row(record)
            if row is None:
                skipped += 1
                continue
            if is_expired(row["response_deadline"], today):
                expired += 1
                if args.skip_expired:
                    continue
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                n, c = upsert_many(conn, batch, run_id=run_id)
                stored, changed = stored + n, changed + c
                batch.clear()
                log.info("  stored %d titles so far (%d seen)", stored, seen)
        n, c = upsert_many(conn, batch, run_id=run_id)
        stored, changed = stored + n, changed + c
    except KeyboardInterrupt:
        n, c = upsert_many(conn, batch, run_id=run_id)
        stored, changed = stored + n, changed + c
        error = "interrupted"
        log.warning("Interrupted -- keeping what was already stored.")
    except RateLimited as exc:
        n, c = upsert_many(conn, batch, run_id=run_id)
        stored, changed = stored + n, changed + c
        error = "rate limited"
        print(f"\nDaily API quota used up. Access returns at {exc.retry_after or 'midnight UTC'}.")
        print("Everything fetched before the limit is saved.")
    except ApiError as exc:
        n, c = upsert_many(conn, batch, run_id=run_id)
        stored, changed = stored + n, changed + c
        error = str(exc)
        log.error("%s", exc)
    finally:
        finish_run(
            conn,
            run_id,
            seen=seen,
            stored=stored,
            api_requests=client.request_count,
            error=error,
        )
        record_requests(conn, "sync", client.request_count)

    # An archived notice is simply absent, never flagged.
    archived = swept = 0
    if not error:
        archived = deactivate_missing(
            conn, posted_from.isoformat(), posted_to.isoformat(), run_started, run_id
        )
        # Outside the window, retire from the stated archive date instead.
        if not args.no_sweep:
            swept = sweep_archived(conn, today, run_id)

    # Skipped on error: a partial sync is no time to delete.
    purged: dict[str, int] = {}
    if not error and not args.no_purge:
        cutoff = retention_cutoff(today, args.retain_years)
        purged = prune_older_than(conn, cutoff, today, keep_open=not args.purge_open)

    total, live = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(active), 0) FROM opportunities"
    ).fetchone()
    print(
        f"\nSeen {seen} notices, stored {stored}, skipped {skipped} "
        f"({client.request_count} API requests)."
    )
    print(f"  {expired} had passed deadlines ({'not stored' if args.skip_expired else 'stored'}).")
    if archived:
        print(f"  {archived} no longer returned by the API; marked inactive.")
    if swept:
        print(f"  {swept} past their own archive date; marked inactive (inferred, no API call).")
    if purged.get("opportunities"):
        print(f"  Purged {describe_prune(purged)} posted over {args.retain_years} years ago.")
    if changed:
        print(f"\n{changed} field change(s) detected on existing notices:")
        for field, n in conn.execute(
            """SELECT field, COUNT(*) FROM changes WHERE run_id = ?
               GROUP BY field ORDER BY COUNT(*) DESC""",
            (run_id,),
        ):
            print(f"  {n:>6}  {field}")
        print("  (see `python main.py changes`)")
    print(f"Database holds {total} notices ({live} still active) -> {Path(args.db).resolve()}")
    conn.close()
    if error == "interrupted":
        # Not a fault, but not success either: exiting 0 here once let a caller
        # treat a stopped sync as finished and launch the next step anyway.
        return INTERRUPTED_EXIT
    return 1 if error else 0


def cmd_changes(args: argparse.Namespace) -> int:
    conn = connect(args.db)

    where, params = [], []
    if args.field:
        where.append("c.field = ?")
        params.append(args.field)
    if args.notice:
        where.append("c.notice_id = ?")
        params.append(args.notice)
    if args.since:
        where.append("c.detected_at >= ?")
        params.append(args.since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    rows = conn.execute(
        f"""SELECT c.detected_at, c.notice_id, c.field, c.old_value, c.new_value,
                   c.source, o.title, o.ui_link
            FROM changes c LEFT JOIN opportunities o USING (notice_id)
            {clause} ORDER BY c.id DESC LIMIT ?""",
        [*params, args.limit],
    ).fetchall()

    if not rows:
        print("No changes recorded yet. Changes appear from the second sync onward.")
        conn.close()
        return 0

    for row in rows:
        title = (row["title"] or "(notice no longer stored)")[:66]
        print(f"\n{row['detected_at'][:16]}  {title}")
        note = {
            "missing": "  [the API stopped returning it]",
            "archive_date": "  [inferred from archive date]",
        }.get(row["source"], "")
        print(f"  {row['field']}: {row['old_value']!r} -> {row['new_value']!r}{note}")
        if args.links and row["ui_link"]:
            print(f"  {row['ui_link']}")

    total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    print(f"\nShowing {len(rows)} of {total:,} recorded changes.")
    conn.close()
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    api_key = resolve_api_key(args)
    conn = connect(args.db)
    today = date.today().isoformat()

    # the API sends no rate-limit headers, so count our own. Syncs count too.
    sync_used, desc_used = requests_used_today(conn)
    if args.cap <= 0:
        # No ceiling: run until the API says stop.
        remaining = 10 ** 9
        print(f"Today so far: {sync_used} sync + {desc_used} description requests. "
              f"No local cap -- running until the API refuses.")
    else:
        remaining = max(0, args.cap - sync_used - desc_used - args.reserve)
        print(f"Today: {sync_used} sync + {desc_used} description requests of {args.cap} "
              f"(reserving {args.reserve}) -> {remaining} available")
        if not remaining:
            print("Daily cap reached. Nothing fetched.")
            conn.close()
            return 0

    queue = description_queue(conn, today, remaining)
    if not queue:
        print("Nothing to fetch: every notice already has a current description.")
        conn.close()
        return 0

    client = ApiClient(api_key, sleep_between=args.sleep, stop_on_rate_limit=True)
    print(f"Fetching {len(queue)} descriptions, most worth reading first…")

    fetched = failed = empty = 0
    interrupted = False
    limited: RateLimited | None = None
    try:
        for i, row in enumerate(queue, 1):
            try:
                text = client.description(row["notice_id"])
            except RateLimited as exc:
                limited = exc
                break
            except ApiError as exc:
                failed += 1
                log.warning("  %s: %s", row["notice_id"][:8], str(exc)[:110])
                continue
            save_description(conn, row["notice_id"], text, row["posted_date"])
            fetched += 1
            if not text:
                empty += 1
            if i % 100 == 0:
                print(f"  {i}/{len(queue)} …")
    except KeyboardInterrupt:
        print("\nInterrupted -- everything fetched so far is saved.")
        interrupted = True
    finally:
        # Before any reporting: a stop or a refusal still spent what it spent.
        record_requests(conn, "desc", client.request_count)

    # Two figures, because the queue no longer stops at the open ones: the first
    # is how much work is left in total, the second how much of it is biddable.
    outstanding = conn.execute(
        "SELECT COUNT(*) FROM opportunities o LEFT JOIN descriptions d USING (notice_id)"
        " WHERE d.notice_id IS NULL OR d.posted_at_fetch < o.posted_date"
    ).fetchone()[0]
    left = conn.execute(
        "SELECT COUNT(*) FROM opportunities o LEFT JOIN descriptions d USING (notice_id)"
        " WHERE o.active=1 AND o.response_deadline >= ? AND d.notice_id IS NULL", (today,)
    ).fetchone()[0]
    print(f"\nFetched {fetched} ({empty} empty, {failed} failed) in {client.request_count} requests.")
    if limited:
        total = sync_used + desc_used + client.request_count
        print("\n*** RATE LIMITED -- this is the real ceiling ***")
        print(f"    stopped after ~{total} requests today (UTC day)")
        print(f"    Retry-After header: {limited.retry_after or 'not sent'}")
        if limited.body:
            print(f"    response body: {limited.body[:200]}")
        print("    Remaining work stays queued; re-run after the window resets.")
    print(f"Still without a description: {outstanding:,} in all, {left:,} of them open.")
    print("Run `python main.py score` to re-rank with the new text.")
    conn.close()
    return INTERRUPTED_EXIT if interrupted else 0


def cmd_score(args: argparse.Namespace) -> int:
    from lib.scoring import Scorer, load_profile

    conn = connect(args.db)
    scorer = Scorer(load_profile(args.profile))

    rows = conn.execute(
        "SELECT o.*, d.text AS description FROM opportunities o"
        " LEFT JOIN descriptions d USING (notice_id)"
    ).fetchall()
    batch = []
    with_desc = 0
    for row in rows:
        description = row["description"]
        if description:
            with_desc += 1
        score, reasons = scorer.score(row, description)
        batch.append((row["notice_id"], score, json.dumps(reasons)))

    write_scores(conn, batch, ranker="rules")

    print(f"Scored {len(batch):,} notices against {Path(args.profile).name} "
          f"({with_desc:,} using full descriptions)\n")
    print_bands(conn)
    print_top(conn, args.top)
    conn.close()
    return 0


def print_bands(conn) -> None:
    for band, count in conn.execute(
        """SELECT CASE WHEN score >= 60 THEN 'strong (60+)'
                       WHEN score >= 40 THEN 'possible (40-59)'
                       WHEN score >= 20 THEN 'weak (20-39)'
                       ELSE 'no (under 20)' END AS band,
                  COUNT(*) FROM scores GROUP BY band ORDER BY MIN(score) DESC"""
    ):
        print(f"  {count:>7,}  {band}")


def print_top(conn, limit: int) -> None:
    print("\nTop matches that are still open:")
    for row in conn.execute(
        """SELECT o.title, o.classification_code, o.response_deadline, s.score
           FROM scores s JOIN opportunities o USING (notice_id)
           WHERE o.active = 1 AND o.response_deadline >= ?
           ORDER BY s.score DESC, o.response_deadline LIMIT ?""",
        (date.today().isoformat(), limit),
    ):
        deadline = (row["response_deadline"] or "")[:10]
        code = row["classification_code"] or "----"
        print(f"  {row['score']:>5.0f}  {deadline}  {code:<5}  {row['title'][:58]}")


def cmd_rocchio(args: argparse.Namespace) -> int:
    from lib import rocchio
    from lib.scoring import Scorer, load_profile

    conn = connect(args.db)
    profile = load_profile(args.profile)
    # Corpus boilerplate is tuning, so it lives in the profile, not the source.
    # Must land before the index is built -- that is where tokenising happens.
    rocchio.extend_stopwords(profile.get("stopwords", []))

    print("Building the term index…")
    corpus = rocchio.Corpus.build(
        rocchio.iter_docs(conn),
        title_weight=args.title_weight,
        min_df=args.min_df,
        slope=args.slope,
    )
    if not len(corpus):
        print("Nothing stored yet -- run `python main.py sync` first.")
        conn.close()
        return 1
    print(f"  {len(corpus):,} notices, {len(corpus.vocab):,} terms")

    where = {notice_id: i for i, notice_id in enumerate(corpus.ids)}
    kept = [where[n] for n in kept_ids(conn) if n in where]
    hidden = [where[n] for n in hidden_ids(conn) if n in where]

    query = rocchio.build_query(
        corpus,
        corpus.seed(profile),
        kept,
        hidden,
        alpha=args.alpha,
        beta=args.beta,
        gamma=args.gamma,
        prior=args.prior,
    )
    if not query:
        print("Empty query: no profile term appears in the corpus, and there is "
              "no feedback to fall back on.")
        conn.close()
        return 1

    # Effective, not nominal: a centroid of two notices does not get full weight.
    pull = args.beta * rocchio.confidence(len(kept), args.prior)
    push = args.gamma * rocchio.confidence(len(hidden), args.prior)
    print(f"Query: {args.alpha:g}x profile seed"
          f" + {pull:.2f}x {len(kept)} kept"
          f" - {push:.2f}x {len(hidden)} hidden")
    if not kept and not hidden:
        print("  (no feedback yet, so this is the profile seed alone -- star and "
              "hide notices in the dashboard, then run this again)")
    elif min(len(kept), len(hidden)) < args.prior:
        print(f"  (feedback is weighted by how much of it there is, half at "
              f"{args.prior:g} examples -- keep labelling and it takes over)")

    cosines = corpus.cosines(query)

    # The rule scores are only the ladder the cosines are mapped onto; they are
    # recomputed rather than read, so `scores` can already hold Rocchio's own.
    scorer = Scorer(profile)
    rule_scores = [
        scorer.score(row, row["description"])[0]
        for row in conn.execute(
            "SELECT o.*, d.text AS description FROM opportunities o "
            "LEFT JOIN descriptions d USING (notice_id)"
        )
    ]
    scores = (rocchio.calibrate(cosines, rule_scores) if not args.raw
              else [min(100.0, max(0.0, c * 100)) for c in cosines])

    batch = []
    for i, notice_id in enumerate(corpus.ids):
        score = scores[i]
        reasons: list = []
        if score > 0 and cosines[i] > 0:
            if kept or hidden:
                reasons.append(
                    [f"similarity to {len(kept)} kept / {len(hidden)} hidden", 0])
            # Contributions sum to the cosine; rescale so they sum to the score.
            scale = score / cosines[i]
            for term, value in corpus.contributions(i, query)[: args.terms]:
                points = round(value * scale)
                if points:
                    reasons.append([term, points])
        batch.append((notice_id, score, json.dumps(reasons)))

    write_scores(conn, batch, ranker="rocchio")

    print(f"\nRanked {len(batch):,} notices by cosine similarity"
          f"{' (raw, uncalibrated)' if args.raw else ''}\n")
    print_bands(conn)
    print_top(conn, args.top)
    print("\n`python main.py score` switches back to the rule ranker.")
    conn.close()
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    conn = connect(args.db)

    ids = list(args.notice or [])
    if args.search:
        ids += [
            row[0] for row in conn.execute(
                "SELECT notice_id FROM opportunities WHERE title LIKE ? "
                "ORDER BY posted_date DESC LIMIT ?",
                (f"%{args.search}%", args.search_limit),
            )
        ]

    if ids:
        label = None if args.clear else args.label
        changed = set_feedback(conn, ids, label)
        verb = {"keep": "kept", "hide": "hidden", None: "cleared"}[label]
        print(f"{changed} notice(s) {verb}.")
    elif args.search:
        print("No titles matched.")

    counts = feedback_counts(conn)
    print(f"\n{counts['keep']} kept, {counts['hide']} hidden.")

    rows = conn.execute(
        """SELECT f.label, f.at, f.score_at, o.title
           FROM feedback f JOIN opportunities o USING (notice_id)
           ORDER BY f.at DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()
    if rows:
        print("\nMost recent:")
        for row in rows:
            was = f"{row['score_at']:>3.0f}" if row["score_at"] is not None else "  -"
            mark = "*" if row["label"] == "keep" else "x"
            print(f"  {mark} {row['at'][:10]}  was {was}  {row['title'][:56]}")
    if counts["keep"] + counts["hide"]:
        print("\nRun `python main.py rocchio` to re-rank against this.")
    conn.close()
    return 0


def describe_prune(counts: dict[str, int]) -> str:
    """'1,514 notices (1,514 scores)' -- child rows only when there were any."""
    n = counts["opportunities"]
    extra = ", ".join(f"{c:,} {t}" for t, c in counts.items()
                      if t != "opportunities" and c)
    return f"{n:,} notice{'' if n == 1 else 's'}" + (f" ({extra})" if extra else "")


def cmd_prune(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    today = date.today().isoformat()
    before = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

    if args.expired:
        counts = prune_expired(conn, today)
        what = f"deadlines before {today}"
    else:
        cutoff = retention_cutoff(today, args.years)
        counts = prune_older_than(conn, cutoff, today, keep_open=not args.purge_open)
        what = f"posted before {cutoff} ({args.years} years back)"
        if not args.purge_open:
            what += ", deadline passed or absent"

    removed = counts["opportunities"]
    print(f"Pruned {describe_prune(counts)} with {what}.")
    print(f"{before - removed:,} notices remain (was {before:,}).")

    # Not during a sync: it rewrites the whole file.
    size_before = Path(args.db).stat().st_size
    conn.execute("VACUUM")
    size_after = Path(args.db).stat().st_size
    print(f"Database {size_before/1e6:.1f} MB -> {size_after/1e6:.1f} MB.")

    left = {t: n for t, n in orphaned_child_rows(conn).items() if n}
    if left:
        print("Warning: orphaned rows remain: "
              + ", ".join(f"{n:,} in {t}" for t, n in left.items()))
    conn.close()
    return 0


def cmd_titles(args: argparse.Namespace) -> int:
    conn = connect(args.db)

    where = [] if args.all else ["active = 1"]
    params: list[object] = []
    if args.search:
        where.append("title LIKE ?")
        params.append(f"%{args.search}%")
    if args.agency:
        where.append("agency LIKE ?")
        params.append(f"%{args.agency}%")

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        "SELECT title, agency, notice_type, posted_date, response_deadline, ui_link "
        f"FROM opportunities {clause} ORDER BY posted_date DESC, title LIMIT ?"
    )
    rows = conn.execute(sql, [*params, args.limit]).fetchall()

    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(rows[0].keys() if rows else ["title"])
        writer.writerows([tuple(row) for row in rows])
    else:
        for row in rows:
            posted = (row["posted_date"] or "")[:10]
            print(f"{posted}  {row['title']}")
        print(f"\n{len(rows)} title(s).")

    conn.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    active, total = conn.execute(
        "SELECT COALESCE(SUM(active), 0), COUNT(*) FROM opportunities"
    ).fetchone()
    print(f"Database:  {Path(args.db).resolve()}")
    print(f"Titles:    {total} total, {active} active")

    top = conn.execute(
        """SELECT agency, COUNT(*) AS n FROM opportunities WHERE active = 1
           GROUP BY agency ORDER BY n DESC LIMIT 10"""
    ).fetchall()
    if top:
        print("\nTop agencies (active):")
        for row in top:
            print(f"  {row['n']:>6}  {row['agency'] or '(unknown)'}")

    runs = conn.execute(
        "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if runs:
        print("\nRecent syncs:")
        for row in runs:
            # No finished_at means the process died before it could close the
            # row -- reporting that as "ok" is how stalled syncs hid in here.
            if row["error"]:
                status = row["error"]
            elif row["finished_at"]:
                status = "ok"
            else:
                status = "running or killed"
            print(
                f"  {row['started_at']}  {row['posted_from']}..{row['posted_to']}  "
                f"seen={row['seen']} stored={row['stored']} [{status}]"
            )

    conn.close()
    return 0


# -- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: data/notices.db)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="fetch titles from the API into the database")
    sync.add_argument("--api-key", help="API key (default: $API_KEY or .env)")
    sync.add_argument("--days", type=int, default=None, help="how far back to look (default: 365)")
    sync.add_argument(
        "--daily",
        action="store_true",
        help="preset for a scheduled run: 2-day window (~4 requests). Catches new "
        "postings and amendments, since an amendment re-dates the notice.",
    )
    sync.add_argument(
        "--no-sweep",
        action="store_true",
        help="skip retiring notices whose archive date has passed",
    )
    sync.add_argument(
        "--retain-years",
        type=int,
        default=RETENTION_YEARS,
        metavar="N",
        help=f"delete notices posted more than N years ago (default: {RETENTION_YEARS})",
    )
    sync.add_argument(
        "--no-purge",
        action="store_true",
        help="keep notices older than the retention window",
    )
    sync.add_argument(
        "--purge-open",
        action="store_true",
        help="also purge old notices whose deadline is still ahead (default: keep "
        "them -- multi-year BAAs stay biddable long after they were posted)",
    )
    sync.add_argument(
        "--since-last",
        action="store_true",
        help="start from where the last successful sync ended, minus a 1-day "
        "buffer (falls back to --days if there is no previous run)",
    )
    sync.add_argument("--from", dest="from", help="start date, YYYY-MM-DD (overrides --days)")
    sync.add_argument("--to", help="end date, YYYY-MM-DD (default: today)")
    sync.add_argument("--limit", type=int, default=MAX_LIMIT, help="page size, max 1000")
    sync.add_argument(
        "--cap", type=int, default=DAILY_QUOTA, metavar="N",
        help=f"skip the run when N API requests have already been spent today, "
             f"syncs and descriptions together (default: {DAILY_QUOTA}); 0 sends "
             "the request anyway and lets the API refuse it",
    )
    sync.add_argument("--sleep", type=float, default=0.25, help="seconds between API requests")
    sync.add_argument(
        "--ptype",
        action="append",
        help="notice type code to fetch, repeatable (o=solicitation, p=presolicitation, "
        "k=combined synopsis, r=sources sought, a=award). Default: all types.",
    )
    sync.add_argument(
        "--skip-expired",
        action="store_true",
        help="don't store notices whose response deadline has already passed "
        "(default: store everything and filter in the dashboard)",
    )
    sync.set_defaults(func=cmd_sync)

    prune = sub.add_parser(
        "prune",
        help="delete old notices and reclaim the space",
        description="By default, enforces the retention window: deletes notices "
        "posted more than N years ago, along with their scores, descriptions and "
        "change history, then VACUUMs. Sync applies the same cutoff automatically; "
        "this is for running it on demand or with a different window.",
    )
    prune.add_argument(
        "--years",
        type=int,
        default=RETENTION_YEARS,
        metavar="N",
        help=f"retention window (default: {RETENTION_YEARS})",
    )
    prune.add_argument(
        "--purge-open",
        action="store_true",
        help="also purge old notices whose deadline is still ahead (default: keep them)",
    )
    prune.add_argument(
        "--expired",
        action="store_true",
        help="instead, delete every notice whose response deadline has passed "
        "-- a much bigger cut, and not what sync does",
    )
    prune.set_defaults(func=cmd_prune)

    changes = sub.add_parser("changes", help="show edits the API made to notices we already had")
    changes.add_argument("--field", help="only this field, e.g. response_deadline")
    changes.add_argument("--notice", help="only this notice id")
    changes.add_argument("--since", help="only changes detected on/after this timestamp")
    changes.add_argument("--limit", type=int, default=25)
    changes.add_argument("--links", action="store_true", help="print the notice link too")
    changes.set_defaults(func=cmd_changes)

    describe = sub.add_parser(
        "describe", help="fetch full descriptions for open notices, newest deadline first")
    describe.add_argument("--api-key")
    describe.add_argument("--cap", type=int, default=0,
                          help="total API requests allowed per UTC day, syncs included; "
                               "0 (the default) means no local ceiling -- fetch until the API refuses")
    describe.add_argument("--reserve", type=int, default=20,
                          help="requests to leave unspent as headroom (default: 20, ignored when --cap is 0)")
    describe.add_argument("--sleep", type=float, default=0.15)
    describe.set_defaults(func=cmd_describe)

    score = sub.add_parser("score", help="rank stored notices against profile.json")
    score.add_argument("--profile", default=str(Path(__file__).parent / "profile.json"))
    score.add_argument("--top", type=int, default=15, help="how many top matches to print")
    score.set_defaults(func=cmd_score)

    rocchio = sub.add_parser(
        "rocchio",
        help="rank by similarity to what you kept, away from what you hid",
        description="Rocchio relevance feedback. Builds a TF-IDF query from "
        "profile.json plus the notices you starred and hid in the dashboard, "
        "then orders every notice by cosine similarity to it. Writes the same "
        "`scores` table the rule ranker does, so the dashboard needs no "
        "changes; `score` switches back.",
    )
    rocchio.add_argument("--profile", default=str(Path(__file__).parent / "profile.json"),
                         help="seeds the query, so this works before any feedback exists")
    rocchio.add_argument("--alpha", type=float, default=ROCCHIO_ALPHA,
                         help=f"weight on the profile seed (default: {ROCCHIO_ALPHA})")
    rocchio.add_argument("--beta", type=float, default=ROCCHIO_BETA,
                         help=f"weight on the kept notices (default: {ROCCHIO_BETA})")
    rocchio.add_argument("--gamma", type=float, default=ROCCHIO_GAMMA,
                         help=f"weight pushing away from hidden notices (default: {ROCCHIO_GAMMA})")
    rocchio.add_argument("--prior", type=float, default=ROCCHIO_PRIOR, metavar="N",
                         help="examples at which feedback gets half its nominal "
                              "weight; 0 trusts the first label completely "
                              f"(default: {ROCCHIO_PRIOR:g})")
    rocchio.add_argument("--min-df", type=int, default=ROCCHIO_MIN_DF, metavar="N",
                         help=f"ignore terms in fewer than N notices (default: {ROCCHIO_MIN_DF})")
    rocchio.add_argument("--title-weight", type=float, default=ROCCHIO_TITLE_WEIGHT,
                         metavar="W",
                         help="how much more a title word counts than a description "
                              f"word (default: {ROCCHIO_TITLE_WEIGHT:g})")
    rocchio.add_argument("--slope", type=float, default=ROCCHIO_SLOPE, metavar="S",
                         help="length normalisation: 1.0 is plain cosine, which "
                              "flatters four-word commodity titles; lower values "
                              f"make short titles earn it (default: {ROCCHIO_SLOPE:g})")
    rocchio.add_argument("--terms", type=int, default=8, metavar="N",
                         help="terms to record per notice as the score breakdown")
    rocchio.add_argument("--raw", action="store_true",
                         help="store cosine x 100 instead of mapping onto the rule "
                              "scores' distribution -- truer numbers, but the "
                              "dashboard's 20/40/60 bands stop meaning anything")
    rocchio.add_argument("--top", type=int, default=15, help="how many top matches to print")
    rocchio.set_defaults(func=cmd_rocchio)

    feedback = sub.add_parser(
        "feedback",
        help="show or set the keep/hide verdicts the ranker learns from",
        description="The dashboard's * and X buttons write this table. Doing it "
        "from here is for bulk work -- hiding a whole family of recurring "
        "commodity buys in one go, which is the fastest way out of a cold start.",
    )
    feedback.add_argument("notice", nargs="*", help="notice ids to label")
    feedback.add_argument("--search", help="also label every stored notice whose title matches")
    feedback.add_argument("--search-limit", type=int, default=200, metavar="N",
                          help="cap on how many --search matches to label (default: 200)")
    feedback.add_argument("--hide", dest="label", action="store_const", const="hide",
                          default="hide", help="label them hidden (the default)")
    feedback.add_argument("--keep", dest="label", action="store_const", const="keep",
                          help="label them kept")
    feedback.add_argument("--clear", action="store_true", help="remove the verdict instead")
    feedback.add_argument("--limit", type=int, default=15, help="how many recent verdicts to show")
    feedback.set_defaults(func=cmd_feedback)

    titles = sub.add_parser("titles", help="list stored titles")
    titles.add_argument("--search", help="substring match on the title")
    titles.add_argument("--agency", help="substring match on the agency")
    titles.add_argument("--limit", type=int, default=50)
    titles.add_argument("--all", action="store_true", help="include inactive notices")
    titles.add_argument("--csv", action="store_true", help="emit CSV instead of plain text")
    titles.set_defaults(func=cmd_titles)

    stats = sub.add_parser("stats", help="summarize what is stored")
    stats.set_defaults(func=cmd_stats)

    return parser


def _interrupt(signum: int, frame: object) -> None:
    """Turn SIGTERM into the same unwind Ctrl-C already gets.

    Without this a `kill` drops us before the commit-and-close handlers in
    cmd_sync/cmd_describe, losing the pending batch and leaving the sync_runs
    row open forever -- which last_synced_to() skips, so the next --since-last
    refetches the same window from scratch.
    """
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).parent / ".env")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        return args.func(args)
    except ApiError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # sync and describe absorb this themselves after saving; anything that
        # reaches here (score, rocchio) had nothing half-written to salvage.
        print("\nStopped.", file=sys.stderr)
        return INTERRUPTED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
