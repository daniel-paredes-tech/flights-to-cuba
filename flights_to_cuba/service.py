"""Fetch from the API and store in PostgreSQL. Shared by the server and the importer."""

import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from .client import AviationstackClient, AviationstackError, RequestBudgetExceeded, RetriesExhausted
from .parser import normalize_flights
from .repository import FlightRepository, ImportSummary

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_and_store(
    repo: FlightRepository,
    client_factory: Callable[[], AviationstackClient],
    airport: str = "HAV",
    now: Callable[[], datetime] = utc_now,
    save_raw: Optional[Callable[[list], None]] = None,
) -> Tuple[Optional[ImportSummary], Optional[str]]:
    """Call the API once and import the response. Every attempt is recorded.

    Returns (summary, None) on success or (None, error message) on failure.
    """
    attempted_at = now()
    client = None
    try:
        client = client_factory()
        raw = client.flights_to(airport, max_pages=1)
    except (AviationstackError, RetriesExhausted, RequestBudgetExceeded, ValueError) as e:
        reason = str(e)
        if client is not None and client.api_key:
            reason = reason.replace(client.api_key, "***")
        log.error("Fetching from the API failed: %s", reason)
        repo.record_fetch_attempt(airport, attempted_at, success=False, error=reason)
        return None, reason

    if save_raw is not None:
        save_raw(raw)
    summary = repo.import_result(raw, normalize_flights(raw), source=f"api:flights?arr_iata={airport}",
                                 fetched_at=attempted_at)
    repo.record_fetch_attempt(airport, attempted_at, success=True, batch_id=summary.batch_id)
    log.info("API import: %s", summary)
    return summary, None
