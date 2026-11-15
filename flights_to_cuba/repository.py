"""Repository layer: the only place in the code that reads or writes PostgreSQL.

Idempotency:
  * The same API payload imported twice is detected by its SHA-256
    (`import_batches.payload_sha256`) and nothing is written the second time.
  * A flight always maps to the same row through `flights.natural_key`
    (`INSERT ... ON CONFLICT DO UPDATE`). The row is only rewritten when a
    value actually changed and the incoming data is not older than the stored
    data, so re-running imports never creates duplicates or churn.
  * Codeshares, live positions and rejected records use composite primary
    keys with `ON CONFLICT DO NOTHING`.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Airline, Airport, Flight
from .parser import NormalizeResult

SCHEMA_PATH = Path(__file__).parent / "db" / "schema.sql"
SCHEMA_LOCK_ID = 727_001  # pg_advisory_xact_lock key for migrations

# Columns compared to decide whether a flight row really changed.
FLIGHT_COLUMNS = [
    "flight_date", "status", "airline_id", "flight_number", "flight_iata", "flight_icao",
    "departure_iata", "departure_scheduled_local", "departure_scheduled_utc",
    "departure_estimated_local", "departure_estimated_utc", "departure_actual_local",
    "departure_actual_utc", "departure_terminal", "departure_gate", "departure_delay_min",
    "arrival_iata", "arrival_scheduled_local", "arrival_scheduled_utc",
    "arrival_estimated_local", "arrival_estimated_utc", "arrival_actual_local",
    "arrival_actual_utc", "arrival_terminal", "arrival_gate", "arrival_baggage",
    "arrival_delay_min", "aircraft_registration", "aircraft_type", "warnings",
]

_UPSERT_FLIGHT = f"""
INSERT INTO flights (natural_key, {", ".join(FLIGHT_COLUMNS)}, source_fetched_at, last_batch_id)
VALUES (%(natural_key)s, {", ".join(f"%({c})s" for c in FLIGHT_COLUMNS)}, %(source_fetched_at)s, %(last_batch_id)s)
ON CONFLICT (natural_key) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in FLIGHT_COLUMNS)},
    source_fetched_at = EXCLUDED.source_fetched_at,
    last_batch_id = EXCLUDED.last_batch_id,
    updated_at = now()
WHERE EXCLUDED.source_fetched_at >= flights.source_fetched_at
  AND ({", ".join(f"flights.{c}" for c in FLIGHT_COLUMNS)})
      IS DISTINCT FROM ({", ".join(f"EXCLUDED.{c}" for c in FLIGHT_COLUMNS)})
RETURNING id, (xmax = 0) AS inserted
"""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url


def connect(url: Optional[str] = None, **kwargs) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), connect_timeout=5, **kwargs)


def payload_hash(raw: Any) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ImportSummary:
    batch_id: int
    already_imported: bool
    flights: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: int = 0

    def __str__(self) -> str:
        if self.already_imported:
            return f"batch {self.batch_id}: already imported, nothing written"
        return (f"batch {self.batch_id}: {self.flights} flights · inserted {self.inserted} · "
                f"updated {self.updated} · unchanged {self.unchanged} · rejected {self.rejected}")


class FlightRepository:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # ---------------------------------------------------------------- schema
    def migrate(self) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            cur.execute(SCHEMA_PATH.read_text())

    # ---------------------------------------------------------------- writes
    def _upsert_airport(self, cur, airport: Airport) -> None:
        cur.execute(
            """
            INSERT INTO airports (iata, icao, name, timezone) VALUES (%s, %s, %s, %s)
            ON CONFLICT (iata) DO UPDATE SET
                icao = COALESCE(EXCLUDED.icao, airports.icao),
                name = COALESCE(EXCLUDED.name, airports.name),
                timezone = COALESCE(EXCLUDED.timezone, airports.timezone)
            WHERE (airports.icao, airports.name, airports.timezone) IS DISTINCT FROM
                  (COALESCE(EXCLUDED.icao, airports.icao), COALESCE(EXCLUDED.name, airports.name),
                   COALESCE(EXCLUDED.timezone, airports.timezone))
            """,
            (airport.iata, airport.icao, airport.name, airport.timezone),
        )

    def _airline_id(self, cur, airline: Airline) -> Optional[int]:
        if not (airline.iata or airline.icao or airline.name):
            return None
        if airline.iata or airline.icao:
            cur.execute(
                """
                INSERT INTO airlines (iata, icao, name) VALUES (%s, %s, %s)
                ON CONFLICT (iata, icao) WHERE iata IS NOT NULL OR icao IS NOT NULL DO UPDATE
                    SET name = COALESCE(EXCLUDED.name, airlines.name)
                    WHERE airlines.name IS DISTINCT FROM COALESCE(EXCLUDED.name, airlines.name)
                RETURNING id
                """,
                (airline.iata, airline.icao, airline.name),
            )
            lookup = ("SELECT id FROM airlines WHERE iata IS NOT DISTINCT FROM %s "
                      "AND icao IS NOT DISTINCT FROM %s", (airline.iata, airline.icao))
        else:
            cur.execute(
                """
                INSERT INTO airlines (name) VALUES (%s)
                ON CONFLICT (name) WHERE iata IS NULL AND icao IS NULL DO NOTHING
                RETURNING id
                """,
                (airline.name,),
            )
            lookup = ("SELECT id FROM airlines WHERE iata IS NULL AND icao IS NULL AND name = %s",
                      (airline.name,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(*lookup)  # already stored and unchanged
        return cur.fetchone()[0]

    @staticmethod
    def _flight_params(f: Flight, airline_id: Optional[int], fetched_at: datetime, batch_id: int) -> Dict[str, Any]:
        def local(w):
            return w.local if w else None

        def utc(w):
            return w.utc if w else None

        d, a = f.departure, f.arrival
        return {
            "natural_key": f.natural_key,
            "flight_date": f.flight_date,
            "status": f.status.value,
            "airline_id": airline_id,
            "flight_number": f.flight_number,
            "flight_iata": f.flight_iata,
            "flight_icao": f.flight_icao,
            "departure_iata": d.airport.iata,
            "departure_scheduled_local": local(d.scheduled), "departure_scheduled_utc": utc(d.scheduled),
            "departure_estimated_local": local(d.estimated), "departure_estimated_utc": utc(d.estimated),
            "departure_actual_local": local(d.actual), "departure_actual_utc": utc(d.actual),
            "departure_terminal": d.terminal, "departure_gate": d.gate, "departure_delay_min": d.delay_min,
            "arrival_iata": a.airport.iata,
            "arrival_scheduled_local": local(a.scheduled), "arrival_scheduled_utc": utc(a.scheduled),
            "arrival_estimated_local": local(a.estimated), "arrival_estimated_utc": utc(a.estimated),
            "arrival_actual_local": local(a.actual), "arrival_actual_utc": utc(a.actual),
            "arrival_terminal": a.terminal, "arrival_gate": a.gate, "arrival_baggage": a.baggage,
            "arrival_delay_min": a.delay_min,
            "aircraft_registration": f.aircraft_registration,
            "aircraft_type": f.aircraft_type,
            "warnings": list(f.warnings),
            "source_fetched_at": fetched_at,
            "last_batch_id": batch_id,
        }

    def import_result(self, raw: Any, result: NormalizeResult, source: str, fetched_at: datetime) -> ImportSummary:
        """Store one normalized API response atomically (all or nothing)."""
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        digest = payload_hash(raw)
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_batches (payload_sha256, source, fetched_at, record_count, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (payload_sha256) DO NOTHING
                RETURNING id
                """,
                (digest, source, fetched_at, len(raw) if isinstance(raw, list) else 0, Jsonb(raw)),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT id FROM import_batches WHERE payload_sha256 = %s", (digest,))
                return ImportSummary(batch_id=cur.fetchone()[0], already_imported=True)
            batch_id = row[0]
            summary = ImportSummary(batch_id=batch_id, already_imported=False, flights=len(result.flights))

            for f in result.flights:
                self._upsert_airport(cur, f.departure.airport)
                self._upsert_airport(cur, f.arrival.airport)
                airline_id = self._airline_id(cur, f.airline)
                cur.execute(_UPSERT_FLIGHT, self._flight_params(f, airline_id, fetched_at, batch_id))
                row = cur.fetchone()
                if row is None:  # nothing changed (or stored data is newer)
                    summary.unchanged += 1
                    cur.execute("SELECT id FROM flights WHERE natural_key = %s", (f.natural_key,))
                    flight_id = cur.fetchone()[0]
                else:
                    flight_id, inserted = row
                    if inserted:
                        summary.inserted += 1
                    else:
                        summary.updated += 1

                for code in f.codeshares:
                    cur.execute("INSERT INTO flight_codeshares (flight_id, flight_iata) VALUES (%s, %s) "
                                "ON CONFLICT DO NOTHING", (flight_id, code))
                live = f.live
                if live is not None and live.updated is not None:
                    cur.execute(
                        """
                        INSERT INTO live_positions (flight_id, observed_at, latitude, longitude, altitude,
                            direction, speed_horizontal, speed_vertical, is_ground)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (flight_id, live.updated, live.latitude, live.longitude, live.altitude,
                         live.direction, live.speed_horizontal, live.speed_vertical, live.is_ground),
                    )

            for r in result.rejected:
                cur.execute("INSERT INTO rejected_records (batch_id, record_index, reason, raw) "
                            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            (batch_id, r.index, r.reason, Jsonb(r.raw)))
            summary.rejected = len(result.rejected)

            cur.execute(
                """
                UPDATE import_batches SET flight_count = %s, inserted_count = %s, updated_count = %s,
                    unchanged_count = %s, rejected_count = %s
                WHERE id = %s
                """,
                (summary.flights, summary.inserted, summary.updated, summary.unchanged, summary.rejected, batch_id),
            )
        return summary

    # ----------------------------------------------------------------- reads
    def count_flights(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM flights")
            return cur.fetchone()[0]

    def list_flights(self, arrival_iata: str = "HAV", since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Flights arriving at `arrival_iata`, scheduled at or after `since` (UTC)."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT f.*, a.name AS airline_name, a.iata AS airline_iata, a.icao AS airline_icao,
                       dep.name AS departure_airport_name, arr.name AS arrival_airport_name,
                       arr.timezone AS arrival_timezone,
                       COALESCE(array_agg(c.flight_iata ORDER BY c.flight_iata)
                                FILTER (WHERE c.flight_iata IS NOT NULL), '{}') AS codeshares
                FROM flights f
                JOIN airports dep ON dep.iata = f.departure_iata
                JOIN airports arr ON arr.iata = f.arrival_iata
                LEFT JOIN airlines a ON a.id = f.airline_id
                LEFT JOIN flight_codeshares c ON c.flight_id = f.id
                WHERE f.arrival_iata = %(airport)s
                  AND (%(since)s::timestamptz IS NULL
                       OR COALESCE(f.arrival_scheduled_utc, f.arrival_scheduled_local AT TIME ZONE 'UTC')
                          >= %(since)s::timestamptz)
                GROUP BY f.id, a.id, dep.iata, arr.iata
                ORDER BY f.arrival_scheduled_utc NULLS LAST, f.arrival_scheduled_local
                """,
                {"airport": arrival_iata, "since": since},
            )
            return cur.fetchall()

    def record_fetch_attempt(self, airport: str, attempted_at: datetime, success: bool,
                             error: Optional[str] = None, batch_id: Optional[int] = None) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_fetch_attempts (airport, attempted_at, success, error, batch_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (airport, attempted_at, success, error, batch_id),
            )

    def freshness(self, airport: str = "HAV") -> Dict[str, Any]:
        """When data was last obtained successfully, and the last API attempt.

        `last_success_at` covers both imports (`import_batches.fetched_at`) and
        API calls that returned an already-imported payload.
        """
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT GREATEST(
                           (SELECT max(fetched_at) FROM import_batches),
                           (SELECT max(attempted_at) FROM api_fetch_attempts
                             WHERE airport = %(airport)s AND success)
                       ) AS last_success_at,
                       last.attempted_at AS last_attempt_at,
                       last.success AS last_attempt_success,
                       last.error AS last_attempt_error
                FROM (SELECT 1) AS one
                LEFT JOIN LATERAL (
                    SELECT attempted_at, success, error FROM api_fetch_attempts
                    WHERE airport = %(airport)s ORDER BY attempted_at DESC, id DESC LIMIT 1
                ) AS last ON true
                """,
                {"airport": airport},
            )
            return cur.fetchone()

    def latest_batch(self) -> Optional[Dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, source, fetched_at, imported_at, record_count, flight_count, inserted_count, "
                        "updated_count, unchanged_count, rejected_count FROM import_batches "
                        "ORDER BY imported_at DESC, id DESC LIMIT 1")
            return cur.fetchone()
