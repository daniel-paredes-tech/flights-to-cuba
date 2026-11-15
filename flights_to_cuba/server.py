"""Private local web page with the flights stored in PostgreSQL.

    docker compose up -d
    python -m flights_to_cuba.server --open      # http://localhost:8000

The page reads from the database through `FlightRepository`. Quota protection
(aviationstack free plan: 100 requests/month):
  * Visiting the page calls the API only if the newest data is >= 12 h old.
  * The "Refresh" button works only if the data is >= 3 h old.
  * After a failed call, the API is not retried for 3 h (5 min if there is no
    data at all), so a broken key or an outage can't drain the quota.
  * Every API call is recorded in `api_fetch_attempts`, so these rules hold
    across restarts and also count calls made by the importer.
  * A lock makes simultaneous requests share a single API call.
"""

import argparse
import errno
import json
import logging
import sys
import threading
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit

import psycopg

from .config import load_dotenv
from .client import AviationstackClient
from .page import flight_from_row, iso, parse_iso, render_html, utc_now
from .repository import FlightRepository, connect
from .service import fetch_and_store

log = logging.getLogger(__name__)

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}
EMPTY_RETRY_GAP = timedelta(minutes=5)


class FlightStore:
    """Flights in PostgreSQL, refreshed from the API only when allowed."""

    def __init__(
        self,
        connect_fn: Callable[[], psycopg.Connection],
        client_factory: Callable[[], AviationstackClient],
        airport: str = "HAV",
        max_age: timedelta = timedelta(hours=12),
        min_refresh: timedelta = timedelta(hours=3),
        history: timedelta = timedelta(hours=24),
        now: Callable[[], datetime] = utc_now,
    ):
        self.connect_fn = connect_fn
        self.client_factory = client_factory
        self.airport = airport
        self.max_age = max_age
        self.min_refresh = min_refresh
        self.history = history
        self.now = now
        self._lock = threading.Lock()

    @contextmanager
    def _repo(self) -> Iterator[FlightRepository]:
        with self.connect_fn() as conn:
            yield FlightRepository(conn)

    def migrate(self) -> None:
        with self._repo() as repo:
            repo.migrate()

    # ---------------------------------------------------------------- policy
    def next_call_at(self, data: Optional[Dict[str, Any]], threshold: timedelta) -> Optional[datetime]:
        """When the API may be called next (None = now)."""
        generated = parse_iso((data or {}).get("generated_at"))
        attempt = parse_iso((data or {}).get("last_attempt_at"))
        candidates = []
        if generated is not None:
            candidates.append(generated + threshold)
        if attempt is not None and (generated is None or attempt > generated):
            gap = self.min_refresh if generated is not None else EMPTY_RETRY_GAP
            candidates.append(attempt + gap)
        when = max(candidates) if candidates else None
        return when if when is not None and when > self.now() else None

    def _state(self, repo: FlightRepository) -> Dict[str, Any]:
        f = repo.freshness(self.airport)
        failed = f["last_attempt_at"] is not None and not f["last_attempt_success"]
        stale = failed and (f["last_success_at"] is None or f["last_attempt_at"] > f["last_success_at"])
        return {
            "airport": self.airport,
            "source": "postgresql",
            "generated_at": iso(f["last_success_at"]) if f["last_success_at"] else None,
            "last_attempt_at": iso(f["last_attempt_at"]) if f["last_attempt_at"] else None,
            "stale": stale,
            "stale_reason": f["last_attempt_error"] if stale else None,
        }

    def _payload(self, repo: FlightRepository) -> Dict[str, Any]:
        payload = self._state(repo)
        since = self.now() - self.history
        payload["showing_since"] = iso(since)
        payload["flights"] = [flight_from_row(r) for r in repo.list_flights(self.airport, since=since)]
        return payload

    def _maybe_fetch(self, threshold: timedelta) -> Tuple[Dict[str, Any], bool]:
        with self._lock, self._repo() as repo:
            if self.next_call_at(self._state(repo), threshold) is not None:
                return self._payload(repo), False
            log.info("Data missing or older than %s: calling the API", threshold)
            fetch_and_store(repo, self.client_factory, self.airport, now=self.now)
            return self._payload(repo), True

    def get(self) -> Dict[str, Any]:
        """Data for a page view: refreshes only if >= 12 h old (and not backing off)."""
        return self._maybe_fetch(self.max_age)[0]

    def refresh(self) -> Tuple[Dict[str, Any], bool]:
        """Manual refresh: allowed only if data is >= 3 h old. Returns (data, refreshed)."""
        return self._maybe_fetch(self.min_refresh)


# ------------------------------------------------------------------- HTTP
def _is_local(host_header: Optional[str]) -> bool:
    if not host_header:
        return False
    host = urlsplit("//" + host_header).hostname or ""
    return host in LOCAL_HOSTNAMES or f"[{host}]" in LOCAL_HOSTNAMES


def make_handler(store: FlightStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "flights-to-cuba"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: str, content_type: str, extra: Optional[Dict[str, str]] = None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _guard(self) -> bool:
            # Blocks DNS-rebinding (foreign Host) and cross-site form posts (foreign Origin).
            if not _is_local(self.headers.get("Host")):
                self._send(HTTPStatus.FORBIDDEN, "Forbidden", "text/plain; charset=utf-8")
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "null" and not _is_local(urlsplit(origin).netloc):
                self._send(HTTPStatus.FORBIDDEN, "Forbidden", "text/plain; charset=utf-8")
                return False
            return True

        def _page(self, data: Dict[str, Any], message: Optional[str] = None):
            next_at = store.next_call_at(data, store.min_refresh)
            self._send(HTTPStatus.OK, render_html(data, now=store.now(), next_refresh_at=next_at, message=message),
                       "text/html; charset=utf-8")

        def do_GET(self):
            if not self._guard():
                return
            try:
                self._get()
            except psycopg.Error as e:
                self._db_down(e)

        def _db_down(self, e: Exception):
            log.error("Database error: %s", e.__class__.__name__)
            self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                       "The database is not available. Start it with: docker compose up -d",
                       "text/plain; charset=utf-8")

        def _get(self):
            path = urlsplit(self.path).path
            if path == "/":
                query = urlsplit(self.path).query
                data = store.get()
                message = None
                if query == "refreshed=1" and not data["stale"]:
                    message = "Data refreshed from the API."
                elif query == "refreshed=0":
                    message = "Not refreshed: the data is less than 3 hours old (protecting your monthly quota)."
                self._page(data, message)
            elif path == "/flights.json":
                self._send(HTTPStatus.OK, json.dumps(store.get(), indent=2, ensure_ascii=False),
                           "application/json; charset=utf-8")
            elif path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, "", "text/plain")
            else:
                self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            if not self._guard():
                return
            if urlsplit(self.path).path != "/refresh":
                self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
                return
            try:
                _, refreshed = store.refresh()
            except psycopg.Error as e:
                self._db_down(e)
                return
            self._send(HTTPStatus.SEE_OTHER, "", "text/plain",
                       {"Location": f"/?refreshed={1 if refreshed else 0}"})

    return Handler


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Private local page with flights arriving in Cuba")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--airport", default="HAV", help="Arrival IATA code")
    p.add_argument("--max-age-hours", type=float, default=12.0, help="Auto-refresh when data is older than this")
    p.add_argument("--min-refresh-hours", type=float, default=3.0, help="Manual refresh allowed after this")
    p.add_argument("--history-hours", type=float, default=24.0, help="Show flights scheduled to arrive since this many hours ago")
    p.add_argument("--open", action="store_true", help="Open the page in the browser")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(Path.cwd() / ".env")

    store = FlightStore(
        connect,
        lambda: AviationstackClient(max_requests=3),
        airport=args.airport,
        max_age=timedelta(hours=args.max_age_hours),
        min_refresh=timedelta(hours=args.min_refresh_hours),
        history=timedelta(hours=args.history_hours),
    )
    try:
        store.migrate()
    except (psycopg.Error, RuntimeError) as e:
        print(f"Cannot reach the database ({e.__class__.__name__}: {e}). "
              "Start it with: docker compose up -d", file=sys.stderr)
        return 1

    # 127.0.0.1 only: the page is not reachable from other devices on the network.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"Port {args.port} is already in use (is the server already running?). "
                  f"Stop the other process or try --port {args.port + 1}", file=sys.stderr)
            return 1
        raise
    url = f"http://localhost:{args.port}/"
    print(f"Serving {url} (only on this computer). Ctrl+C to stop.", file=sys.stderr)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
