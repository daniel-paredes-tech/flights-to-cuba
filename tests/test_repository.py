"""Integration tests against the local PostgreSQL (docker compose up -d).

They use a throwaway `flights_test` database and are skipped when
DATABASE_URL is not set or the server is not reachable.
"""

import copy
import unittest
from datetime import datetime, timedelta, timezone

from flights_to_cuba.parser import normalize_flights
from tests.test_parser import MU4978, UX51, rec

from tests.db import SKIP, TEST_URL, reset_database

if not SKIP:
    import psycopg

    from flights_to_cuba.repository import FlightRepository, payload_hash

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

# A third flight with no codes at all (seen in real data as airline "empty").
NO_CODE = rec(airline={"name": "empty", "iata": None, "icao": None},
              flight={"number": None, "iata": None, "icao": None, "codeshared": None},
              departure__iata="CUN", departure__timezone="America/Cancun",
              departure__scheduled="2026-11-15T08:42:00+00:00",
              arrival__scheduled="2026-11-15T10:30:00+00:00")
WITH_LIVE = rec(flight={"number": "1", "iata": "UX1", "icao": "AEA1", "codeshared": None},
                live={"latitude": 23.1, "longitude": -82.4, "altitude": 1000, "is_ground": False,
                      "updated": "2026-11-15T17:02:38+00:00"})
BAD = {"departure": {"iata": "MAD"}}


@unittest.skipIf(SKIP, SKIP or "")
class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(TEST_URL)
        self.repo = FlightRepository(self.conn)
        reset_database(self.conn)

    def tearDown(self):
        self.conn.close()

    def importing(self, raw, at=T0):
        return self.repo.import_result(raw, normalize_flights(raw), source="test", fetched_at=at)

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_migrate_is_idempotent(self):
        self.repo.migrate()
        self.repo.migrate()
        self.assertEqual(self.conn.execute("SELECT version FROM schema_migrations ORDER BY 1").fetchall(), [(1,), (2,)])

    def test_first_import(self):
        s = self.importing([UX51, MU4978, NO_CODE, WITH_LIVE, BAD])
        self.assertEqual((s.inserted, s.updated, s.unchanged, s.rejected), (3, 0, 0, 1))
        self.assertEqual(self.count("flights"), 3)
        self.assertEqual(self.count("flight_codeshares"), 1)
        self.assertEqual(self.count("live_positions"), 1)
        self.assertEqual(self.count("rejected_records"), 1)
        self.assertEqual(self.count("airports"), 3)

    def test_same_payload_twice_writes_nothing(self):
        raw = [UX51, MU4978, NO_CODE, WITH_LIVE, BAD]
        first = self.importing(raw)
        second = self.importing(copy.deepcopy(raw), at=T0 + timedelta(hours=1))
        self.assertTrue(second.already_imported)
        self.assertEqual(second.batch_id, first.batch_id)
        self.assertEqual(self.count("import_batches"), 1)
        self.assertEqual(self.count("flights"), 3)

    def test_different_payload_same_flights_no_duplicates(self):
        self.importing([UX51, MU4978, NO_CODE, WITH_LIVE])
        # Reordered + one extra rejected record: a new payload with the same flights.
        s = self.importing([WITH_LIVE, NO_CODE, MU4978, UX51, BAD], at=T0 + timedelta(hours=1))
        self.assertFalse(s.already_imported)
        self.assertEqual((s.inserted, s.updated, s.unchanged), (0, 0, 3))
        self.assertEqual(self.count("flights"), 3)
        self.assertEqual(self.count("flight_codeshares"), 1)
        self.assertEqual(self.count("live_positions"), 1)
        # Only operating airlines are stored (MU4978 is a codeshare of UX51) and never duplicated.
        self.assertEqual(self.count("airlines"), 1)
        dupes = self.conn.execute("SELECT natural_key FROM flights GROUP BY 1 HAVING count(*) > 1").fetchall()
        self.assertEqual(dupes, [])

    def test_changed_flight_is_updated_in_place(self):
        self.importing([UX51])
        before = self.conn.execute("SELECT id, updated_at FROM flights").fetchone()
        s = self.importing([rec(flight_status="landed")], at=T0 + timedelta(hours=1))
        self.assertEqual((s.inserted, s.updated), (0, 1))
        after = self.conn.execute("SELECT id, status, updated_at FROM flights").fetchone()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], "landed")
        self.assertGreater(after[2], before[1])

    def test_older_data_does_not_overwrite_newer(self):
        self.importing([rec(flight_status="landed")], at=T0)
        s = self.importing([UX51], at=T0 - timedelta(hours=5))  # older snapshot, status "scheduled"
        self.assertEqual(s.unchanged, 1)
        self.assertEqual(self.conn.execute("SELECT status FROM flights").fetchone()[0], "landed")

    def test_list_flights(self):
        self.importing([UX51, MU4978, NO_CODE])
        rows = self.repo.list_flights("HAV")
        self.assertEqual([r["flight_iata"] for r in rows], [None, "UX51"])
        self.assertEqual(rows[1]["codeshares"], ["MU4978"])
        self.assertEqual(rows[1]["airline_name"], "Air Europa")
        self.assertEqual(rows[1]["arrival_scheduled_utc"], datetime(2026, 11, 16, 1, 5, tzinfo=timezone.utc))

    def test_failed_import_rolls_back(self):
        from unittest import mock
        with mock.patch.object(FlightRepository, "_upsert_airport", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.importing([UX51])
        self.assertEqual(self.count("import_batches"), 0)
        self.assertEqual(self.count("flights"), 0)

    def test_payload_hash_ignores_key_order(self):
        self.assertEqual(payload_hash([{"a": 1, "b": 2}]), payload_hash([{"b": 2, "a": 1}]))


class NaturalKeyTests(unittest.TestCase):
    def test_keys(self):
        flights = {f.flight_iata: f for f in normalize_flights([UX51, NO_CODE], dedupe=False).flights}
        self.assertEqual(flights["UX51"].natural_key, "UX51|2026-11-15|MAD")
        self.assertEqual(flights[None].natural_key, "CUN-HAV|dep=2026-11-15T08:42")


if __name__ == "__main__":
    unittest.main()
