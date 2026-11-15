"""Resilient client for the aviationstack API.

Handles:
  * connect/read timeouts on every request
  * retries with exponential backoff + jitter on transient failures
    (timeouts, network errors, HTTP 429/5xx, JSON error `rate_limit_reached`)
  * rate limiting: minimum interval between requests, honours `Retry-After`
    and caps the number of requests per run to protect the monthly quota.
"""

import logging
import os
import random
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

log = logging.getLogger(__name__)

# HTTPS by default. The free plan only supports HTTP: set
# AVIATIONSTACK_BASE_URL=http://api.aviationstack.com/v1 in that case.
DEFAULT_BASE_URL = "https://api.aviationstack.com/v1"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Error codes aviationstack returns inside the JSON body.
RETRYABLE_API_ERRORS = {"rate_limit_reached", "internal_error", "timeout"}


class AviationstackError(Exception):
    """Non-recoverable API error (invalid key, quota exhausted, ...)."""

    def __init__(self, code: str, message: str, status: Optional[int] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class RetriesExhausted(Exception):
    """Retries ran out on a transient failure."""


class RequestBudgetExceeded(Exception):
    """The maximum number of requests for this run was reached."""


class _TransientError(Exception):
    def __init__(self, reason: str, retry_after: Optional[float] = None):
        super().__init__(reason)
        self.retry_after = retry_after


class RateLimiter:
    """Enforces a minimum interval between requests (thread-safe)."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: Optional[float] = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                remaining = self.min_interval - (now - self._last)
                if remaining > 0:
                    self._sleep(remaining)
                    now = self._clock()
            self._last = now

    def penalize(self, seconds: float) -> None:
        """Delay the next request (e.g. after a 429 with Retry-After)."""
        with self._lock:
            self._last = self._clock() + seconds - self.min_interval


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date format: ignore it and fall back to backoff


class AviationstackClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        min_interval: float = 1.0,
        max_requests: Optional[int] = None,
        session: Optional[requests.Session] = None,
        sleep=time.sleep,
    ):
        self.api_key = api_key or os.environ.get("AVIATIONSTACK_API_KEY")
        if not self.api_key:
            raise ValueError("Missing API key (AVIATIONSTACK_API_KEY).")
        self.base_url = (base_url or os.environ.get("AVIATIONSTACK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = (connect_timeout, read_timeout)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_requests = max_requests
        self.requests_made = 0
        self.session = session or requests.Session()
        self._sleep = sleep
        self.rate_limiter = RateLimiter(min_interval, sleep=sleep)

    # ------------------------------------------------------------------ core
    def _backoff(self, attempt: int) -> float:
        # "Full jitter": random between 0 and base * 2^attempt (capped).
        return random.uniform(0, min(self.backoff_max, self.backoff_base * (2 ** attempt)))

    def _request_once(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(f"Limit of {self.max_requests} requests reached")
        self.rate_limiter.wait()
        self.requests_made += 1
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            resp = self.session.get(url, params={**params, "access_key": self.api_key}, timeout=self.timeout)
        except requests.Timeout as e:
            raise _TransientError(f"timeout: {e.__class__.__name__}")
        except requests.ConnectionError as e:
            raise _TransientError(f"connection error: {e.__class__.__name__}")

        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        try:
            body = resp.json()
        except ValueError:
            body = None

        error = body.get("error") if isinstance(body, dict) else None
        if error:
            code = str(error.get("code", "unknown"))
            message = str(error.get("message", ""))
            if code in RETRYABLE_API_ERRORS or resp.status_code in RETRYABLE_STATUS:
                raise _TransientError(f"API {code} (HTTP {resp.status_code})", retry_after)
            raise AviationstackError(code, message, resp.status_code)

        if resp.status_code in RETRYABLE_STATUS:
            raise _TransientError(f"HTTP {resp.status_code}", retry_after)
        if resp.status_code >= 400:
            raise AviationstackError(f"http_{resp.status_code}", resp.text[:200], resp.status_code)
        if not isinstance(body, dict):
            raise _TransientError("response is not valid JSON")
        return body

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        for attempt in range(self.max_retries + 1):
            try:
                return self._request_once(endpoint, params)
            except _TransientError as e:
                if attempt == self.max_retries:
                    raise RetriesExhausted(f"{endpoint}: {e} after {attempt + 1} attempts") from e
                delay = e.retry_after if e.retry_after is not None else self._backoff(attempt)
                log.warning("Transient failure (%s). Retry %d/%d in %.1fs",
                            e, attempt + 1, self.max_retries, delay)
                if e.retry_after is not None:
                    self.rate_limiter.penalize(delay)
                else:
                    self._sleep(delay)
        raise AssertionError("unreachable")

    # --------------------------------------------------------------- helpers
    def iter_flights(self, max_pages: int = 1, limit: int = 100, **filters) -> Iterator[Dict[str, Any]]:
        """Iterate over /flights results, paginating with limit/offset."""
        offset = 0
        for _ in range(max_pages):
            data = self.get("flights", {**filters, "limit": limit, "offset": offset})
            items = data.get("data") or []
            yield from items
            pag = data.get("pagination") or {}
            total = pag.get("total")
            offset += len(items)
            if not items or (total is not None and offset >= total):
                break

    def flights_to(self, arr_iata: str, max_pages: int = 1, **filters) -> List[Dict[str, Any]]:
        return list(self.iter_flights(max_pages=max_pages, arr_iata=arr_iata, **filters))
