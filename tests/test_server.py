"""Tests for the local web page: refresh rules, HTTP endpoints and HTML rendering."""

import http.client
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

from flights_to_cuba.client import AviationstackError
from flights_to_cuba.page import render_html
from tests.db import SKIP, TEST_URL, reset_database
from tests.test_parser import UX51, rec

if not SKIP:
    import psycopg

    from flights_to_cuba.server import FlightStore, make_handler

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@unittest.skipIf(SKIP, SKIP or "")
class StoreTestCase(unittest.TestCase):
    def setUp(self):
        with psycopg.connect(TEST_URL) as conn:
            reset_database(conn)
        self.clock = Clock()
        self.client = mock.Mock(api_key="secret-key", requests_made=1)
        # Each call returns a fresh copy with a different status so payloads differ.
        self.client.flights_to.side_effect = lambda *a, **k: [rec(flight_status="active")]
        self.store = FlightStore(lambda: psycopg.connect(TEST_URL), lambda: self.client, now=self.clock)
        self.store.migrate()

    @property
    def calls(self):
        return self.client.flights_to.call_count

    def db(self, sql):
        with psycopg.connect(TEST_URL) as conn:
            return conn.execute(sql).fetchall()


class StoreTests(StoreTestCase):
    def test_first_visit_fetches_and_stores_in_db(self):
        data = self.store.get()
        self.assertEqual(self.calls, 1)
        self.assertEqual(data["source"], "postgresql")
        self.assertEqual(data["flights"][0]["flight_iata"], "UX51")
        self.assertEqual(data["flights"][0]["arrival"]["scheduled"]["utc"], "2026-11-16T01:05:00Z")
        self.assertEqual(data["generated_at"], "2026-11-15T12:00:00Z")
        self.assertEqual(self.db("SELECT count(*) FROM flights"), [(1,)])
        self.assertEqual(self.db("SELECT success FROM api_fetch_attempts"), [(True,)])

    def test_visits_within_12h_read_db_only(self):
        self.store.get()
        self.clock.advance(hours=11, minutes=59)
        self.store.get()
        self.assertEqual(self.calls, 1)
        self.clock.advance(minutes=1)
        self.store.get()
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.db("SELECT count(*) FROM flights"), [(1,)])  # no duplicates

    def test_same_payload_still_counts_as_fresh(self):
        self.client.flights_to.side_effect = None
        self.client.flights_to.return_value = [UX51]
        self.store.get()
        self.clock.advance(hours=12)
        self.store.get()  # identical payload -> "already imported", but the call succeeded
        self.clock.advance(hours=1)
        self.store.get()
        self.assertEqual(self.calls, 2)

    def test_manual_refresh_needs_3h(self):
        self.store.get()
        self.clock.advance(hours=2, minutes=59)
        self.assertFalse(self.store.refresh()[1])
        self.clock.advance(minutes=1)
        self.assertTrue(self.store.refresh()[1])
        self.assertEqual(self.calls, 2)

    def test_api_error_keeps_old_data_and_backs_off(self):
        self.store.get()
        self.client.flights_to.side_effect = AviationstackError("usage_limit_reached", "secret-key")
        self.clock.advance(hours=12)
        data = self.store.get()
        self.assertTrue(data["stale"])
        self.assertEqual(data["flights"][0]["flight_iata"], "UX51")
        self.assertNotIn("secret-key", data["stale_reason"])
        self.clock.advance(hours=2)
        self.store.get()
        self.assertFalse(self.store.refresh()[1])
        self.assertEqual(self.calls, 2)  # no retries during the 3 h back-off
        self.clock.advance(hours=1)
        self.store.get()
        self.assertEqual(self.calls, 3)

    def test_no_data_failure_retries_after_5_min(self):
        self.client.flights_to.side_effect = AviationstackError("invalid_access_key", "bad")
        self.assertEqual(self.store.get()["flights"], [])
        self.clock.advance(minutes=4)
        self.store.get()
        self.assertEqual(self.calls, 1)
        self.clock.advance(minutes=1)
        self.store.get()
        self.assertEqual(self.calls, 2)

    def test_old_flights_outside_history_are_hidden(self):
        self.store.get()
        self.clock.advance(days=3)
        self.client.flights_to.side_effect = AviationstackError("usage_limit_reached", "x")
        self.assertEqual(self.store.get()["flights"], [])
        self.assertEqual(self.db("SELECT count(*) FROM flights"), [(1,)])  # still stored

    def test_concurrent_visits_share_one_call(self):
        def slow(*a, **k):
            time.sleep(0.05)
            return [UX51]
        self.client.flights_to.side_effect = slow
        threads = [threading.Thread(target=self.store.get) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.calls, 1)


class HttpTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"localhost:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, headers=h)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp, body

    def test_page_and_json(self):
        with self.assertLogs("flights_to_cuba", level="INFO"):
            resp, body = self.request("GET", "/")
            self.assertEqual(resp.status, 200)
            self.assertIn("UX51", body)
            resp, body = self.request("GET", "/flights.json")
        self.assertEqual(json.loads(body)["flights"][0]["flight_iata"], "UX51")
        self.assertEqual(self.calls, 1)

    def test_foreign_host_rejected(self):
        with self.assertLogs("flights_to_cuba", level="INFO"):
            resp, _ = self.request("GET", "/", {"Host": "evil.example:8000"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.calls, 0)

    def test_cross_site_post_rejected(self):
        with self.assertLogs("flights_to_cuba", level="INFO"):
            resp, _ = self.request("POST", "/refresh", {"Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.calls, 0)

    def test_refresh_redirects(self):
        with self.assertLogs("flights_to_cuba", level="INFO"):
            self.request("GET", "/")
            resp, _ = self.request("POST", "/refresh", {"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/?refreshed=0")
        self.assertEqual(self.calls, 1)

    def test_database_down_returns_503(self):
        def broken():
            raise psycopg.OperationalError("connection refused")
        self.store.connect_fn = broken
        with self.assertLogs("flights_to_cuba", level="INFO"):
            resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 503)
        self.assertIn("docker compose up -d", body)


class RenderTests(unittest.TestCase):
    def test_values_are_escaped(self):
        payload = {"airport": "HAV", "generated_at": "2026-11-15T12:00:00Z", "flights": [
            {"flight_iata": "<script>x</script>", "airline": {"name": "A&B"}, "status": "active",
             "arrival": {"scheduled": {"local": "2026-11-15T20:05:00"}}, "departure": {"iata": "MAD"}}]}
        page = render_html(payload, now=T0)
        self.assertNotIn("<script>x", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("A&amp;B", page)
        self.assertIn("Sun 15 Nov 20:05", page)
        self.assertIn("Refresh now", page)

    def test_refresh_disabled_and_stale_notice(self):
        page = render_html({"airport": "HAV", "generated_at": None, "flights": [], "stale": True,
                            "stale_reason": "usage_limit_reached"}, now=T0, next_refresh_at=T0 + timedelta(hours=3))
        self.assertIn("No flights available", page)
        self.assertIn("usage_limit_reached", page)
        self.assertIn("disabled", page)


if __name__ == "__main__":
    unittest.main()
