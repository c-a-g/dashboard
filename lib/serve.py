"""Local server for the dashboard. The `Notices Dashboard` launchers start this.

    python lib/serve.py        # http://127.0.0.1:8765, --port, --stay
    python lib/serve.py --detach   # background it; what the launchers run

Loopback only; writes require an X-Sync-Request header. Exits once no tab has
pinged for IDLE_TIMEOUT.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Run as a script, so sys.path holds lib/. Add the root for `import lib.x`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.dashboard import FEEDBACK_SLOT        # noqa: E402  (needs the path above)
from lib.store import (                        # noqa: E402
    clear_phase,
    connect,
    get_phase,
    hidden_ids,
    kept_ids,
    requests_used_today,
    set_feedback,
    set_phase,
)

DASHBOARD = ROOT / "data" / "dashboard.html"
DB = ROOT / "data" / "notices.db"
LOG = ROOT / "data" / "serve.log"
PYTHON = sys.executable

# Absolute -- the working directory isn't reliable.
CLI = ROOT / "main.py"
BUILD = HERE / "dashboard.py"

# The slot sits near the top of the file.
HEAD_BYTES = 4096

# One sync at a time.
sync_lock = threading.Lock()

# The step currently running, so /api/stop can signal it, plus the flag that
# keeps the rest of the pipeline from starting once a stop has landed. Both are
# read from the request thread while do_sync() writes them, hence the lock.
proc_lock = threading.Lock()
current_proc: subprocess.Popen | None = None
stop_requested = False

# Which job is in flight and when it began, so a tab that did not start it can
# show the same message and elapsed clock as the tab that did.
job_kind: str | None = None
job_started: float | None = None

# How long a stopped step gets to commit and close its run row before SIGKILL.
STOP_GRACE = 25.0

# Mirrors main.INTERRUPTED_EXIT -- keep the two in step.
INTERRUPTED_EXIT = 130

# The page pings every 15s.
IDLE_TIMEOUT = 60.0
STARTUP_GRACE = 30.0
last_seen = time.monotonic() + STARTUP_GRACE


def run(args: list[str], timeout: int = 900, force: bool = False) -> tuple[int, str]:
    """Run one pipeline step, tracked so a stop request can reach it.

    `force` runs the step even after a stop -- used for the final rebuild, which
    is what makes the page match the rows the stopped step already committed.
    """
    global current_proc
    with proc_lock:
        if stop_requested and not force:
            # A stop landed between steps; don't start another one.
            return INTERRUPTED_EXIT, ""
        proc = subprocess.Popen(
            [PYTHON, *args],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # CREATE_NO_WINDOW: the server has no console to lend a step, so
            # Windows would open one for it and leave it up for the whole sync.
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        current_proc = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Same courtesy a Stop gets: SIGTERM so the step can commit and close its
        # run row, SIGKILL only if it will not go. subprocess.run would have gone
        # straight to SIGKILL, which is how timed-out syncs used to leave their
        # sync_runs row open forever.
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        # The original exception was built at the timeout, before the step said
        # goodbye. Carry what it managed to write so the caller can report it.
        exc.output = (out or "") + (err or "")
        raise
    finally:
        with proc_lock:
            current_proc = None
    return proc.returncode, (out or "") + (err or "")


def request_stop() -> dict:
    """Stop the running step, giving it time to finish writing first."""
    global stop_requested
    with proc_lock:
        proc = current_proc
        # current_proc is briefly None between steps; the lock is what says a
        # job is still in flight, so a click landing in that gap still counts.
        if proc is None and not sync_lock.locked():
            return {"ok": False, "error": "Nothing is running."}
        stop_requested = True
    if proc is None:
        # Between steps -- the flag alone keeps the next one from starting.
        return {"ok": True, "stopped": True, "clean": True}

    # SIGTERM, not SIGKILL: main.py turns it into the same unwind as Ctrl-C, so
    # the pending batch commits and the sync_runs row closes before it exits.
    proc.terminate()
    deadline = time.monotonic() + STOP_GRACE
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {"ok": True, "stopped": True, "clean": True}
        time.sleep(0.2)

    # Wedged mid-write (or ignoring the signal) -- SIGKILL is all that is left.
    # SQLite commits per batch, so the worst case is losing the batch in flight.
    proc.kill()
    return {"ok": True, "stopped": True, "clean": False}


def stopped() -> bool:
    with proc_lock:
        return stop_requested


def _quick(fn, default=None):
    """One short read or write, without store.connect()'s schema and migration.

    Those take a write transaction, which is a poor thing to do several times a
    second against a database a sync is writing to.
    """
    try:
        conn = sqlite3.connect(str(DB), timeout=2.0)
        try:
            return fn(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return default


def clear_job() -> None:
    global job_kind, job_started
    with proc_lock:
        job_kind, job_started = None, None
    _quick(clear_phase)


def set_step(label: str) -> None:
    """Name the step about to run.

    Writes the same row `main.py` writes, so the two take turns on one channel:
    serve names the step it is about to launch, and the step overwrites that with
    whatever it is doing inside itself.
    """
    _quick(lambda conn: set_phase(conn, label))


def job_state() -> dict:
    """What a tab needs to show the running job as if it had started it."""
    with proc_lock:
        kind, started = job_kind, job_started
    # Only meaningful while something holds the lock; a row left behind by a
    # server that died mid-sync would otherwise read as live progress.
    step = _quick(get_phase) if sync_lock.locked() else None
    return {
        "running": sync_lock.locked(),
        "job": kind,
        "step": step,
        "elapsed": round(time.monotonic() - started, 1) if started else 0,
    }


def requests_today() -> dict:
    """API requests spent this UTC day, for the counter in the page header.

    Rides along on the heartbeat rather than getting a poll of its own, and the
    heartbeat speeds up while a job runs so the figure tracks the spend live.

    Opened raw rather than through store.connect(): that applies the schema and
    runs the migration, which takes a write transaction -- fine once per page
    load, wasteful several times a second, and pointless contention with the
    sync writing on the other side. This only ever reads one row. A database
    that will not open is not worth failing a heartbeat over, so the counter
    simply goes quiet.
    """
    counts = _quick(requests_used_today)
    if counts is None:
        return {}
    sync, desc = counts
    return {"sync": sync, "desc": desc, "total": sync + desc}


def stopped_result(log: str, note: str) -> dict:
    """Close out a stopped run: rebuild the page, then report what was kept.

    The stopped step committed as it went, so the DB is consistent but the
    dashboard on disk is stale. Rebuilding is local and free, so it always runs.
    """
    set_step("Rebuilding the page")
    build_code, build_out = run([str(BUILD), "--no-open"], force=True)
    return {
        "ok": False,
        "stopped": True,
        "error": note,
        "log": log + build_out,
        "rebuilt": build_code == 0,
    }


def quota_result(log: str, note: str) -> dict:
    """Close out a run with no allowance left to spend.

    The sweep and the prunes inside the sync step have already run -- they cost
    no request -- so the page on disk is behind the database. Rebuilding is
    local and free, and puts the two back in step.

    Reached two ways: the cap check refusing before a request is sent, and the
    API refusing partway through. The second has notices to show for it.
    """
    set_step("Compacting the database")
    _, vac_out = run([str(CLI), "vacuum"], timeout=600, force=True)
    set_step("Rebuilding the page")
    build_code, build_out = run([str(BUILD), "--no-open"], force=True)
    return {
        "ok": False,
        "quota": True,
        "error": note,
        "log": log + vac_out + build_out,
        "rebuilt": build_code == 0,
    }


def do_sync() -> dict:
    """Sync from the last run's end date, then rebuild the dashboard."""
    global stop_requested, job_kind, job_started
    if not sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "A sync is already running."}
    try:
        with proc_lock:
            stop_requested = False
            job_kind, job_started = "sync", time.monotonic()

        # sync -> descriptions -> rebuild. Ranking has its own button.
        # the API's search endpoint has a fixed per-request latency that can swing
        # from ~3s to ~200s regardless of page size, so a routine 6-page window
        # can take 20+ minutes. The old 900s default killed those runs mid-flight.
        set_step("Syncing notices")
        code, output = run([str(CLI), "sync", "--since-last"], timeout=2700)
        if stopped():
            return stopped_result(output, "Stopped. Notices fetched so far are saved.")
        if quota_message(output):
            return quota_result(output, quota_message(output))
        if code != 0:
            return {"ok": False, "error": output.strip()[-400:], "log": output}

        set_step("Fetching descriptions")
        desc_code, desc_out = run([str(CLI), "describe"], timeout=1800)
        if stopped():
            return stopped_result(
                output + desc_out,
                "Stopped. The sync finished; descriptions fetched so far are saved.",
            )

        # After the pruning, before the page: it needs the database to itself,
        # and a failure here costs nothing worth stopping the run over.
        set_step("Compacting the database")
        _, vac_out = run([str(CLI), "vacuum"], timeout=600)

        set_step("Rebuilding the page")
        build_code, build_out = run([str(BUILD), "--no-open"])
        if build_code != 0:
            return {"ok": False, "error": build_out.strip()[-800:]}

        log = output + desc_out + vac_out + build_out
        return {"ok": True, "log": log,
                "summary": summarize(output) + " · " + summarize(desc_out)}
    except subprocess.TimeoutExpired as exc:
        tail = (exc.output or "").strip()
        return {"ok": False,
                "error": "Sync timed out. " + (tail[-300:] if tail else
                                               "Anything already fetched was saved."),
                "log": tail}
    finally:
        clear_job()
        sync_lock.release()


def do_rerank() -> dict:
    """Re-rank against the current keep/hide labels, then rebuild the page.

    No network and no quota -- just `main.py rocchio` over what is already
    stored, so it can be run as often as you label things.
    """
    global stop_requested, job_kind, job_started
    if not sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "A sync is already running."}
    try:
        # Clear any flag left by a previous stop, or run() would refuse to start.
        with proc_lock:
            stop_requested = False
            job_kind, job_started = "rerank", time.monotonic()

        set_step("Re-ranking")
        code, output = run([str(CLI), "rocchio"])
        if stopped():
            return stopped_result(output, "Stopped. Ranking is unchanged.")
        if code != 0:
            return {"ok": False, "error": output.strip()[-400:], "log": output}

        set_step("Rebuilding the page")
        build_code, build_out = run([str(BUILD), "--no-open"])
        if build_code != 0:
            return {"ok": False, "error": build_out.strip()[-800:]}

        return {"ok": True, "log": output + build_out, "summary": summarize_rank(output)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Re-ranking timed out."}
    finally:
        clear_job()
        sync_lock.release()


def summarize_rank(output: str) -> str:
    """The query line and the count, minus the band table and the top list."""
    keep = [line.strip() for line in output.splitlines()
            if line.startswith(("Query:", "Ranked "))]
    return " · ".join(keep)[:300] or "Ranking updated."


def quota_message(output: str) -> str | None:
    """Plain-English rate-limit message."""
    if "quota" not in output.lower() and "rate limited" not in output.lower():
        return None
    return ("Daily API quota used up (1,000 requests). It resets at midnight UTC "
            "— 8pm Eastern. Everything fetched so far is saved.")


def summarize(output: str) -> str:
    """Result lines from the sync log, minus progress chatter."""
    noise = ("page ", "titles so far", "notices across", "Syncing notices",
             "Resuming from", "Run `python", "…")
    keep = []
    for line in output.splitlines():
        text = line.strip()
        if not text or any(n in text for n in noise):
            continue
        if text.startswith(("Seen ", "Database holds", "Fetched ", "Today:",
                            "Open notices", "Nothing to fetch", "Daily cap",
                            "Purged ")) or text[0].isdigit():
            keep.append(text)
    return " · ".join(keep)[:500] or "Done."


@contextmanager
def db():
    """One connection per request; sqlite3 forbids sharing across threads."""
    conn = connect(DB)
    try:
        yield conn
    finally:
        conn.close()


def dashboard_html() -> bytes:
    """The page, with the live keep/hide lists patched into its boot script.

    The API count rides along for the same reason the verdicts do: the file on
    disk was built at the end of the last sync, so its copy is stale the moment
    anything is spent. Injected here, the header is right in the first paint
    instead of being corrected when the first heartbeat lands a moment later.
    """
    raw = DASHBOARD.read_bytes()
    with db() as conn:
        hidden, kept = hidden_ids(conn), kept_ids(conn)
    # {} means the count could not be read; null falls back to the page's own
    # snapshot rather than reporting a budget of zero spent.
    requests = requests_today() or None

    compact = lambda value: json.dumps(value, separators=(",", ":"))
    head, tail = raw[:HEAD_BYTES], raw[HEAD_BYTES:]
    head = head.replace(
        FEEDBACK_SLOT.encode(),
        f"var SAVED_HIDDEN = {compact(hidden)}, SAVED_KEPT = {compact(kept)}, "
        f"SAVED_REQUESTS = {compact(requests)};".encode(),
    )
    return head + tail


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):        # quieter than the default
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/dashboard.html"):
            if not DASHBOARD.is_file():
                self._send(404, b"Run: python dashboard.py", "text/plain")
                return
            self._send(200, dashboard_html(), "text/html; charset=utf-8")
        elif path == "/api/ping":
            global last_seen
            last_seen = time.monotonic()
            # Carries the job state so a reloaded tab -- or one whose /api/sync
            # request was dropped -- picks the run back up: same message, same
            # elapsed clock, working Stop button.
            self._json(200, {"ok": True, **job_state(), "requests": requests_today()})
        elif path == "/api/status":
            self._json(200, {"today": date.today().isoformat()})
        elif path == "/api/state":
            with db() as conn:
                self._json(200, {"hidden": hidden_ids(conn), "kept": kept_ids(conn)})
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        # A custom header forces a CORS preflight this server won't answer.
        if self.headers.get("X-Sync-Request") != "1":
            self._json(403, {"ok": False, "error": "Missing X-Sync-Request header."})
            return

        if path == "/api/sync":
            result = do_sync()
            self._json(200 if result.get("ok") else 500, result)
        elif path == "/api/rerank":
            result = do_rerank()
            self._json(200 if result.get("ok") else 500, result)
        elif path == "/api/stop":
            # Served on another thread while do_sync() blocks on the step.
            self._json(200, request_stop())
        elif path in ("/api/feedback", "/api/hidden"):
            payload = self._body()
            ids = payload.get("ids") or []
            if not isinstance(ids, list):
                self._json(400, {"ok": False, "error": "ids must be a list."})
                return
            if path == "/api/hidden":
                # The older two-state call, still used by the localStorage import.
                label = "hide" if payload.get("hidden") else None
            else:
                label = payload.get("label") or None
                if label not in ("keep", "hide", None):
                    self._json(400, {"ok": False, "error": f"unknown label {label!r}"})
                    return
            with db() as conn:
                changed = set_feedback(conn, [str(i) for i in ids], label)
                self._json(200, {"ok": True, "changed": changed,
                                 "hidden": hidden_ids(conn), "kept": kept_ids(conn)})
        else:
            self._send(404, b"Not found", "text/plain")


def already_running(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def watchdog(server: ThreadingHTTPServer, timeout: float) -> None:
    """Shut down once no dashboard tab has pinged for `timeout` seconds."""
    while True:
        time.sleep(5)
        if sync_lock.locked():        # never quit part-way through a sync
            continue
        if time.monotonic() - last_seen > timeout:
            print("No dashboard open - shutting down.")
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def detach(extra: list[str]) -> int:
    """Re-launch this server in the background, then return straight away.

    Lives here rather than in the launchers so the three of them can be one
    line each: the child gets its own session -- a detached console on Windows
    -- so closing the terminal or Explorer window that started it does not take
    the server with it. Output goes to the log, truncated per launch like the
    shell launchers this replaces: it records why a start failed, not history.
    """
    LOG.parent.mkdir(parents=True, exist_ok=True)

    python = PYTHON
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        # pythonw runs windowless; python.exe would flash a console up.
        #
        # The base install's copy first, not the venv's. A venv built by uv
        # ships trampolines, and its pythonw.exe starts the base *python.exe* --
        # a console program. Windows 11 hands that console to Windows Terminal,
        # which shows it whatever the launcher asked for and keeps it on screen
        # for as long as the server runs, which is the whole session. The base
        # pythonw.exe has no console to give away. Nothing is lost by stepping
        # outside the venv: everything here is standard library.
        for candidate in (getattr(sys, "_base_executable", ""), python):
            if not candidate:
                continue
            windowless = Path(candidate).with_name("pythonw.exe")
            if windowless.is_file():
                python = str(windowless)
                break
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True      # setsid(2)

    # -u because stdout to a file is block-buffered: without it the log stays
    # empty until the server exits, which is exactly when you don't need it.
    # The child dups this handle; closing our copy here leaves it with its own.
    with open(LOG, "wb") as log:
        subprocess.Popen([python, "-u", str(Path(__file__).resolve()), *extra],
                         stdout=log, **kwargs)
    print("Notices Dashboard starting -- the browser will open in a moment.")
    print(f"Output: {LOG}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument(
        "--detach",
        action="store_true",
        help="start in the background and return; what the launchers use",
    )
    parser.add_argument(
        "--stay",
        action="store_true",
        help="keep running with no dashboard open (default: exit when idle)",
    )
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}/"

    if args.detach:
        # Everything but the flag itself, so --port and friends carry over.
        return detach([a for a in sys.argv[1:] if a != "--detach"])

    # A second double-click just reopens the tab.
    if already_running(args.port):
        print(f"Already running at {url}")
        if not args.no_open:
            webbrowser.open(url)
        return 0

    if not DASHBOARD.is_file():
        print("dashboard.html not found -- building it first...")
        run([str(BUILD), "--no-open"])

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 10048):
            print(f"Port {args.port} is busy; assuming the dashboard is already served.")
            if not args.no_open:
                webbrowser.open(url)
            return 0
        raise

    print(f"Dashboard:  {url}")
    print("Exits automatically when the dashboard is closed." if not args.stay
          else "Staying up until Ctrl-C.")
    if not args.stay:
        threading.Thread(target=watchdog, args=(server, IDLE_TIMEOUT), daemon=True).start()
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
