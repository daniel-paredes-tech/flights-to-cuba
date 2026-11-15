"""Tests for the parser: times and time zones, missing fields, bad records, codeshares."""

import copy
import unittest
from datetime import datetime, timezone

from flights_to_cuba.models import FlightStatus
from flights_to_cuba.parser import RecordError, normalize_flights, parse_flight

# Real (trimmed) aviationstack record: UX51 Madrid -> Havana.
UX51 = {
    "flight_date": "2026-11-15",
    "flight_status": "scheduled",
    "departure": {"airport": "Adolfo Suarez Madrid-Barajas", "timezone": "Europe/Madrid",
                  "iata": "MAD", "icao": "LEMD", "terminal": "2", "gate": None, "delay": 15,
                  "scheduled": "2026-11-15T15:05:00+00:00", "estimated": "2026-11-15T15:05:00+00:00",
                  "actual": None},
    "arrival": {"airport": "Jose Marti International", "timezone": "America/Havana",
                "iata": "HAV", "icao": "MUHA", "terminal": "3", "gate": None, "baggage": None,
                "delay": None, "scheduled": "2026-11-15T20:05:00+00:00", "estimated": None,
                "actual": None},
    "airline": {"name": "Air Europa", "iata": "UX", "icao": "AEA"},
    "flight": {"number": "51", "iata": "UX51", "icao": "AEA51", "codeshared": None},
    "aircraft": None,
    "live": None,
}


def codeshare_of(op, name, iata, icao, number):
    r = copy.deepcopy(op)
    r["airline"] = {"name": name, "iata": iata[:2], "icao": icao}
    r["flight"] = {"number": number, "iata": iata, "icao": icao + number, "codeshared": {
        "airline_name": op["airline"]["name"].lower(), "airline_iata": op["airline"]["iata"].lower(),
        "airline_icao": op["airline"]["icao"].lower(), "flight_number": op["flight"]["number"],
        "flight_iata": op["flight"]["iata"].lower(), "flight_icao": op["flight"]["icao"].lower()}}
    return r


MU4978 = codeshare_of(UX51, "China Eastern Airlines", "MU4978", "CES", "4978")


def rec(**changes):
    r = copy.deepcopy(UX51)
    for path, value in changes.items():
        *parents, last = path.split("__")
        target = r
        for p in parents:
            target = target[p]
        target[last] = value
    return r


class TimeTests(unittest.TestCase):
    def test_fake_utc_offset_is_local_time(self):
        f, _ = parse_flight(UX51)
        dep, arr = f.departure.scheduled, f.arrival.scheduled
        self.assertEqual(dep.local, datetime(2026, 11, 15, 15, 5))
        # Madrid in November = UTC+1; Havana = UTC-5.
        self.assertEqual(dep.utc, datetime(2026, 11, 15, 14, 5, tzinfo=timezone.utc))
        self.assertEqual(arr.utc, datetime(2026, 11, 16, 1, 5, tzinfo=timezone.utc))
        self.assertEqual((arr.utc - dep.utc).total_seconds() / 3600, 11)
        self.assertEqual(f.warnings, ())

    def test_missing_timezone_keeps_local_without_utc(self):
        f, _ = parse_flight(rec(departure__timezone=None))
        self.assertEqual(f.departure.scheduled.local, datetime(2026, 11, 15, 15, 5))
        self.assertIsNone(f.departure.scheduled.utc)
        self.assertTrue(any("departure.timezone" in w for w in f.warnings))

    def test_unknown_timezone(self):
        f, _ = parse_flight(rec(departure__timezone="Mars/Olympus"))
        self.assertIsNone(f.departure.scheduled.utc)
        self.assertTrue(any("unknown time zone" in w for w in f.warnings))

    def test_bad_optional_date_becomes_none(self):
        f, _ = parse_flight(rec(departure__estimated="tomorrow at 3"))
        self.assertIsNone(f.departure.estimated)
        self.assertTrue(any("departure.estimated" in w for w in f.warnings))

    def test_z_suffix_and_naive_times_accepted(self):
        f, _ = parse_flight(rec(arrival__scheduled="2026-11-15T20:05:00Z", departure__scheduled="2026-11-15 15:05:00"))
        self.assertEqual(f.arrival.scheduled.local, datetime(2026, 11, 15, 20, 5))
        self.assertEqual(f.departure.scheduled.local, datetime(2026, 11, 15, 15, 5))

    def test_dst_gap_warns(self):
        # 2026-03-29 02:30 does not exist in Madrid (clocks jump 02:00 -> 03:00).
        f, _ = parse_flight(rec(departure__scheduled="2026-03-29T02:30:00+00:00",
                                arrival__scheduled="2026-03-29T09:00:00+00:00"))
        self.assertTrue(any("nonexistent" in w for w in f.warnings))

    def test_arrival_before_departure_warns(self):
        f, _ = parse_flight(rec(arrival__scheduled="2026-11-15T05:00:00+00:00"))
        self.assertTrue(any("before departure" in w for w in f.warnings))

    def test_missing_flight_date_falls_back(self):
        f, _ = parse_flight(rec(flight_date=None))
        self.assertEqual(f.flight_date.isoformat(), "2026-11-15")


class FieldTests(unittest.TestCase):
    def test_empty_placeholders_become_none(self):
        f, _ = parse_flight(rec(airline={"name": "empty", "iata": None, "icao": ""},
                                flight={"number": None, "iata": None, "icao": None, "codeshared": None}))
        self.assertIsNone(f.airline.name)
        self.assertIsNone(f.airline.icao)
        self.assertIsNone(f.flight_iata)

    def test_codes_are_uppercased_and_validated(self):
        f, _ = parse_flight(rec(airline={"name": "x", "iata": "ux", "icao": "A3"}))
        self.assertEqual(f.airline.iata, "UX")
        self.assertIsNone(f.airline.icao)
        self.assertTrue(any("airline.icao" in w for w in f.warnings))

    def test_delay_parsing(self):
        self.assertEqual(parse_flight(rec(departure__delay="20"))[0].departure.delay_min, 20)
        f, _ = parse_flight(rec(departure__delay="abc"))
        self.assertIsNone(f.departure.delay_min)
        self.assertTrue(f.warnings)

    def test_status(self):
        self.assertEqual(parse_flight(rec(flight_status=None))[0].status, FlightStatus.UNKNOWN)
        self.assertEqual(parse_flight(rec(flight_status="LANDED"))[0].status, FlightStatus.LANDED)
        f, _ = parse_flight(rec(flight_status="teleported"))
        self.assertEqual(f.status, FlightStatus.UNKNOWN)
        self.assertTrue(f.warnings)

    def test_live_with_bad_coordinates_dropped(self):
        f, _ = parse_flight(rec(live={"latitude": 200, "longitude": -82, "updated": "2026-11-15T17:02:38+00:00"}))
        self.assertIsNone(f.live)
        good, _ = parse_flight(rec(live={"latitude": "23.1", "longitude": -82.4, "is_ground": False,
                                         "updated": "2026-11-15T17:02:38+00:00"}))
        self.assertEqual(good.live.latitude, 23.1)
        self.assertEqual(good.live.updated, datetime(2026, 11, 15, 17, 2, 38, tzinfo=timezone.utc))

    def test_to_dict_serializes_utc_with_z(self):
        d = parse_flight(UX51)[0].to_dict()
        self.assertEqual(d["arrival"]["scheduled"], {"local": "2026-11-15T20:05:00", "utc": "2026-11-16T01:05:00Z"})
        self.assertEqual(d["status"], "scheduled")


class RejectionTests(unittest.TestCase):
    def test_required_fields(self):
        for bad in (rec(arrival=None), rec(arrival__iata=None), rec(departure__iata="??"),
                    rec(arrival__scheduled=None), rec(departure="x"), "not a dict", None):
            with self.assertRaises(RecordError):
                parse_flight(bad)

    def test_bad_records_do_not_crash_batch(self):
        raws = [UX51, None, rec(arrival=None), {"departure": [], "arrival": 5},
                rec(flight="weird", airline=["x"], aircraft=7)]
        result = normalize_flights(raws, dedupe=False)
        self.assertEqual([r.index for r in result.rejected], [1, 2, 3])
        self.assertEqual(len(result.flights), 2)
        self.assertIsNone(result.flights[1].flight_iata)

    def test_unexpected_exception_is_contained(self):
        from unittest import mock
        with mock.patch("flights_to_cuba.parser._live", side_effect=RuntimeError("boom")):
            result = normalize_flights([UX51])
        self.assertEqual(len(result.flights), 0)
        self.assertIn("RuntimeError", result.rejected[0].reason)


class CodeshareTests(unittest.TestCase):
    def test_codeshare_merged_into_operator(self):
        result = normalize_flights([MU4978, UX51])
        self.assertEqual(len(result.flights), 1)
        f = result.flights[0]
        self.assertEqual(f.flight_iata, "UX51")
        self.assertEqual(f.codeshares, ("MU4978",))
        self.assertEqual(result.codeshares_merged, 1)

    def test_operator_rebuilt_when_missing(self):
        result = normalize_flights([MU4978])
        f = result.flights[0]
        self.assertEqual((f.flight_iata, f.airline.iata, f.airline.name), ("UX51", "UX", "Air Europa"))
        self.assertEqual(f.codeshares, ("MU4978",))
        self.assertTrue(any("rebuilt" in w for w in f.warnings))

    def test_no_dedupe_keeps_records(self):
        self.assertEqual(len(normalize_flights([MU4978, UX51], dedupe=False).flights), 2)

    def test_sorted_by_arrival(self):
        early = rec(flight={"number": "1", "iata": "UX1", "icao": "AEA1", "codeshared": None},
                    arrival__scheduled="2026-11-15T18:00:00+00:00")
        result = normalize_flights([UX51, early])
        self.assertEqual([f.flight_iata for f in result.flights], ["UX1", "UX51"])


if __name__ == "__main__":
    unittest.main()
