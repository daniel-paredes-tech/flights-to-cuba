"""Tests for the API client: timeouts, retries and rate limiting (no network)."""

import unittest
from unittest import mock

import requests

from flights_to_cuba.client import (
    AviationstackClient,
    AviationstackError,
    RateLimiter,
    RequestBudgetExceeded,
    RetriesExhausted,
)


def resp(status=200, body=None, headers=None):
    r = mock.Mock()
    r.status_code = status
    r.headers = headers or {}
    r.text = str(body)
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


OK = {"pagination": {"limit": 100, "offset": 0, "count": 1, "total": 1}, "data": [{"flight": {"iata": "AA1"}}]}


def make_client(responses, **kw):
    session = mock.Mock()
    session.get.side_effect = responses
    sleeps = []
    client = AviationstackClient(api_key="k", session=session, sleep=sleeps.append,
                                 min_interval=0, **kw)
    return client, session, sleeps


class ClientTests(unittest.TestCase):
    def test_success(self):
        c, s, _ = make_client([resp(200, OK)])
        self.assertEqual(c.flights_to("HAV"), OK["data"])
        _, kwargs = s.get.call_args
        self.assertEqual(kwargs["params"]["arr_iata"], "HAV")
        self.assertEqual(kwargs["timeout"], (5.0, 15.0))

    def test_retries_on_503(self):
        c, s, sleeps = make_client([resp(503, None), resp(200, OK)])
        self.assertEqual(c.get("flights")["data"], OK["data"])
        self.assertEqual(s.get.call_count, 2)
        self.assertEqual(len(sleeps), 1)

    def test_retries_on_timeout_and_connection_error(self):
        c, s, _ = make_client([requests.Timeout(), requests.ConnectionError(), resp(200, OK)])
        c.get("flights")
        self.assertEqual(s.get.call_count, 3)

    def test_retry_after_is_respected(self):
        c, s, _ = make_client([resp(429, {"error": {"code": "rate_limit_reached"}},
                                    {"Retry-After": "7"}), resp(200, OK)])
        with mock.patch.object(c.rate_limiter, "penalize") as pen:
            c.get("flights")
        pen.assert_called_once_with(7.0)

    def test_rate_limit_error_in_200_body_is_retried(self):
        c, s, _ = make_client([resp(200, {"error": {"code": "rate_limit_reached"}}), resp(200, OK)])
        c.get("flights")
        self.assertEqual(s.get.call_count, 2)

    def test_invalid_key_not_retried(self):
        c, s, _ = make_client([resp(401, {"error": {"code": "invalid_access_key", "message": "bad"}})])
        with self.assertRaises(AviationstackError) as cm:
            c.get("flights")
        self.assertEqual(cm.exception.code, "invalid_access_key")
        self.assertEqual(s.get.call_count, 1)

    def test_usage_limit_not_retried(self):
        c, s, _ = make_client([resp(200, {"error": {"code": "usage_limit_reached"}})])
        with self.assertRaises(AviationstackError):
            c.get("flights")
        self.assertEqual(s.get.call_count, 1)

    def test_retries_exhausted(self):
        c, s, _ = make_client([resp(500, None)] * 3, max_retries=2)
        with self.assertRaises(RetriesExhausted):
            c.get("flights")
        self.assertEqual(s.get.call_count, 3)

    def test_request_budget(self):
        c, s, _ = make_client([resp(500, None)] * 5, max_requests=2)
        with self.assertRaises(RequestBudgetExceeded):
            c.get("flights")
        self.assertEqual(s.get.call_count, 2)

    def test_pagination_stops_at_total(self):
        page1 = {"pagination": {"total": 3}, "data": [{}, {}]}
        page2 = {"pagination": {"total": 3}, "data": [{}]}
        c, s, _ = make_client([resp(200, page1), resp(200, page2)])
        self.assertEqual(len(c.flights_to("HAV", max_pages=5, limit=2)), 3)
        self.assertEqual(s.get.call_count, 2)
        self.assertEqual(s.get.call_args[1]["params"]["offset"], 2)


class RateLimiterTests(unittest.TestCase):
    def test_spaces_calls(self):
        t = [100.0]
        sleeps = []

        def sleep(d):
            sleeps.append(d)
            t[0] += d

        rl = RateLimiter(1.0, clock=lambda: t[0], sleep=sleep)
        rl.wait()
        t[0] += 0.3
        rl.wait()
        self.assertAlmostEqual(sleeps[0], 0.7)
        rl.penalize(5)
        rl.wait()
        self.assertAlmostEqual(sleeps[1], 5.0)


if __name__ == "__main__":
    unittest.main()
