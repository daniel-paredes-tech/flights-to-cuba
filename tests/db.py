"""Shared setup for tests that need the local PostgreSQL (docker compose up -d).

Tests use a throwaway `flights_test` database, recreated once per test run.
If DATABASE_URL is not set or the server is unreachable, SKIP holds the reason.
"""

import os
from pathlib import Path

from flights_to_cuba.config import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

TEST_URL = None
try:
    import psycopg
    from psycopg.conninfo import make_conninfo

    _url = os.environ.get("DATABASE_URL")
    if not _url:
        raise RuntimeError("DATABASE_URL not set")
    with psycopg.connect(make_conninfo(_url, dbname="postgres"), connect_timeout=3, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS flights_test WITH (FORCE)")
        c.execute("CREATE DATABASE flights_test")
    TEST_URL = make_conninfo(_url, dbname="flights_test")
    SKIP = None
except Exception as e:  # pragma: no cover - depends on the environment
    SKIP = f"PostgreSQL not available: {e.__class__.__name__}"


def reset_database(conn) -> None:
    """Apply the schema and empty every table."""
    from flights_to_cuba.repository import FlightRepository

    FlightRepository(conn).migrate()
    conn.execute("TRUNCATE flights, airlines, airports, import_batches, flight_codeshares, live_positions, "
                 "rejected_records, api_fetch_attempts RESTART IDENTITY CASCADE")
    conn.commit()
