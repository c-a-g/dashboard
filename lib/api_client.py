"""Client for the upstream notice search API.

Standard library only -- no third-party dependencies.

The two endpoints are read from the environment rather than hardcoded here, so
the source carries no vendor detail. See `.env.example`.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any, Iterator

log = logging.getLogger(__name__)


def endpoint(name: str) -> str:
    """One endpoint from the environment.

    Resolved on use, not at import: main.py reads .env inside main(), long
    after this module has been imported.
    """
    url = os.environ.get(name, "").strip()
    if not url:
        raise ApiError(
            f"{name} is not set.\n"
            f"  Put it in a .env file next to main.py -- see .env.example."
        )
    return url

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&rsquo;": "'",
    "&lsquo;": "'", "&ldquo;": '"', "&rdquo;": '"', "&ndash;": "-", "&mdash;": "-",
    "&lt;": "<", "&gt;": ">",
}


def html_to_text(html: str) -> str:
    """Descriptions come as HTML; scoring wants readable words."""
    text = _TAGS.sub(" ", html or "")
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return _WS.sub(" ", text).strip()

# The API caps page size at 1000 records.
MAX_LIMIT = 1000

# Requests allowed per API key per UTC day. Fixed and known -- the API sends no
# quota headers, so the spend is counted locally against this figure.
DAILY_QUOTA = 1000

# `offset` is a PAGE INDEX, not a record offset: offset=1&limit=1000 returns
# records 1000-1999. Getting this wrong silently returns a sparse sample.

# postedFrom/postedTo must be MM/dd/yyyy and span at most one year.
DATE_FMT = "%m/%d/%Y"
MAX_WINDOW_DAYS = 365


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to IPv4 only.

    The API host publishes AAAA records that accept no connections: they time out
    silently instead of refusing. urllib walks getaddrinfo's list one address at
    a time and spends the whole socket timeout on each, and a machine with
    working IPv6 is handed the three AAAA records first -- so every request paid
    3 x timeout (180s at the default 60) before falling back to IPv4, which then
    connects in ~0.02s. curl hides this with Happy Eyeballs; urllib has no
    equivalent. Restricting the lookup is the fix.

    Harmless if the host ever repairs its IPv6: IPv4 keeps working either way.
    """

    def connect(self) -> None:
        last: OSError | None = None
        for family, socktype, proto, _, addr in socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        ):
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(addr)
            except OSError as exc:
                sock.close()
                last = exc
                continue
            self.sock = sock
            break
        else:
            raise last or OSError(f"no IPv4 address for {self.host}")

        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tunnel_host or self.host
        )


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req, context=self._context)


class ApiError(RuntimeError):
    """The API returned something we cannot use."""


class RateLimited(ApiError):
    """The daily quota is exhausted.

    Raised instead of retrying when the caller wants to discover the real limit
    -- backing off and retrying would hide exactly the signal we're after.
    """

    def __init__(self, retry_after: str | None, body: str = "") -> None:
        self.retry_after = retry_after
        self.body = body
        super().__init__(f"Rate limited (Retry-After: {retry_after or 'not sent'}) {body[:200]}")


class ApiClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 60.0,
        sleep_between: float = 0.25,
        max_retries: int = 5,
        stop_on_rate_limit: bool = False,
    ) -> None:
        self.stop_on_rate_limit = stop_on_rate_limit
        if not api_key:
            raise ApiError("No API key. Set API_KEY or pass --api-key.")
        self.api_key = api_key
        self.timeout = timeout
        self.sleep_between = sleep_between
        self.max_retries = max_retries
        self.request_count = 0
        self._opener = urllib.request.build_opener(_IPv4HTTPSHandler())

    # -- HTTP ---------------------------------------------------------------

    def _get(self, params: dict[str, Any], base: str | None = None) -> dict[str, Any]:
        base = base or endpoint("API_BASE")
        query = urllib.parse.urlencode({**params, "api_key": self.api_key})
        url = f"{base}?{query}"
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "notices/1.0"}
        )

        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    self.request_count += 1
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # Spent either way: the quota is charged for the request, not for
                # liking the answer. A 429 costs one too, which is why a counter
                # built from successes alone can never reach the ceiling -- it
                # goes quiet on the very request that proves the limit is real.
                self.request_count += 1
                body = exc.read().decode("utf-8", "replace")[:500]
                if exc.code in (401, 403):
                    raise ApiError(
                        f"The API rejected the key (HTTP {exc.code}): {body}"
                    ) from exc
                if exc.code == 404 and not body:
                    # A bare 404 with no body means an unrecognised key.
                    raise ApiError(
                        "The API returned an empty 404. This almost always means the "
                        "API key is missing, invalid, or not yet activated -- not a bad URL. "
                        "Check the key with whoever issued it."
                    ) from exc
                if exc.code == 429:
                    if self.stop_on_rate_limit:
                        raise RateLimited(exc.headers.get("Retry-After"), body) from exc
                    wait = float(exc.headers.get("Retry-After") or delay)
                    log.warning("Rate limited; waiting %.0fs (attempt %d)", wait, attempt)
                elif exc.code >= 500:
                    wait = delay
                    log.warning("Server error %d; retrying in %.0fs", exc.code, wait)
                else:
                    raise ApiError(f"HTTP {exc.code} from the API: {body}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                wait = delay
                log.warning("Request failed (%s); retrying in %.0fs", exc, wait)

            if attempt == self.max_retries:
                break
            time.sleep(wait)
            delay = min(delay * 2, 60.0)

        raise ApiError(f"Gave up after {self.max_retries} attempts: {url.split('&api_key')[0]}")

    # -- Paging -------------------------------------------------------------

    def _page(
        self,
        posted_from: date,
        posted_to: date,
        page: int,
        limit: int,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        params = {
            "postedFrom": posted_from.strftime(DATE_FMT),
            "postedTo": posted_to.strftime(DATE_FMT),
            "limit": limit,
            "offset": page,  # page index -- see the note on MAX_LIMIT above
            **extra,
        }
        payload = self._get(params)
        if self.sleep_between:
            time.sleep(self.sleep_between)
        return payload

    def _iter_window(
        self,
        posted_from: date,
        posted_to: date,
        limit: int,
        extra: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """Yield every notice posted in [posted_from, posted_to]."""
        first = self._page(posted_from, posted_to, 0, limit, extra)
        total = int(first.get("totalRecords", 0))
        page_count = -(-total // limit)  # ceil

        log.info("%s..%s: %d notices across %d page(s)", posted_from, posted_to, total, page_count)
        yield from first.get("opportunitiesData") or []

        for page in range(1, page_count):
            batch = self._page(posted_from, posted_to, page, limit, extra).get(
                "opportunitiesData"
            ) or []
            if not batch:
                # Past the end of the result set; nothing more to collect.
                log.debug("page %d of %d came back empty; stopping", page, page_count)
                break
            log.info("  page %d/%d (+%d)", page + 1, page_count, len(batch))
            yield from batch

    def description(self, notice_id: str) -> str:
        """Plain-text description for one notice. Costs one request."""
        payload = self._get({"noticeid": notice_id}, base=endpoint("API_DESC_URL"))
        if self.sleep_between:
            time.sleep(self.sleep_between)
        return html_to_text(payload.get("description") or "")

    def iter_opportunities(
        self,
        posted_from: date,
        posted_to: date,
        *,
        limit: int = MAX_LIMIT,
        notice_types: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw notice records posted between the two dates, inclusive."""
        if posted_from > posted_to:
            raise ApiError(f"postedFrom {posted_from} is after postedTo {posted_to}")

        extra: dict[str, Any] = {}
        limit = max(1, min(limit, MAX_LIMIT))

        # The API caps spans at one year.
        for chunk_start, chunk_end in _year_chunks(posted_from, posted_to):
            if notice_types:
                for notice_type in notice_types:
                    yield from self._iter_window(
                        chunk_start, chunk_end, limit, {**extra, "ptype": notice_type}
                    )
            else:
                yield from self._iter_window(chunk_start, chunk_end, limit, extra)


def _year_chunks(start: date, end: date) -> Iterator[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=MAX_WINDOW_DAYS - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)
