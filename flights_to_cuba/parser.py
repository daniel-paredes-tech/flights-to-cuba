"""Turns the raw aviationstack response into clean `Flight` objects.

Main rules:
  * aviationstack stamps times with `+00:00`, but they are actually the
    airport's LOCAL time. That offset is discarded and the time is interpreted
    with the airport's IANA zone (`departure.timezone` / `arrival.timezone`).
  * A bad record is rejected on its own; it never stops the run.
  * Codeshares are grouped under the operating flight.
"""

import logging
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Airline, Airport, Endpoint, Flight, FlightStatus, LivePosition, When

log = logging.getLogger(__name__)

Warn = Callable[[str], None]

_NULLISH = {"", "empty", "null", "none", "n/a", "na", "-", "unknown"}

AIRPORT_IATA = re.compile(r"^[A-Z]{3}$")
AIRPORT_ICAO = re.compile(r"^[A-Z0-9]{4}$")
AIRLINE_IATA = re.compile(r"^[A-Z0-9]{2}$")
AIRLINE_ICAO = re.compile(r"^[A-Z]{3}$")
FLIGHT_CODE = re.compile(r"^[A-Z0-9]{2,3}[0-9]{1,5}[A-Z]?$")
FLIGHT_NUMBER = re.compile(r"^[0-9]{1,5}[A-Z]?$")


class RecordError(ValueError):
    """The record lacks the minimum data and is rejected."""


@dataclass
class Rejected:
    index: int
    reason: str
    raw: Any

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "reason": self.reason, "raw": self.raw}


@dataclass
class NormalizeResult:
    flights: List[Flight] = field(default_factory=list)
    rejected: List[Rejected] = field(default_factory=list)
    codeshares_merged: int = 0

    @property
    def with_warnings(self) -> int:
        return sum(1 for f in self.flights if f.warnings)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def clean_str(value: Any) -> Optional[str]:
    """Trimmed text; '', 'empty', 'null'... -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return None if value.lower() in _NULLISH else value


def clean_name(value: Any) -> Optional[str]:
    s = clean_str(value)
    # Some names arrive in lowercase ("air europa"): capitalize them.
    return s.title() if s and s.islower() else s


def clean_code(value: Any, pattern: "re.Pattern[str]", path: str, warn: Warn) -> Optional[str]:
    s = clean_str(value)
    if s is None:
        return None
    s = s.upper()
    if not pattern.match(s):
        warn(f"{path}: code with unexpected format {value!r}, discarded")
        return None
    return s


def parse_int(value: Any, path: str, warn: Warn) -> Optional[int]:
    if value is None or (isinstance(value, str) and clean_str(value) is None):
        return None
    if isinstance(value, bool):
        warn(f"{path}: non-numeric value {value!r}")
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        warn(f"{path}: non-numeric value {value!r}")
        return None
    if not math.isfinite(f) or f != int(f):
        warn(f"{path}: non-integer value {value!r}")
        return None
    return int(f)


def parse_float(value: Any, path: str, warn: Warn) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        warn(f"{path}: non-numeric value {value!r}")
        return None
    if not math.isfinite(f):
        warn(f"{path}: non-finite value {value!r}")
        return None
    return f


def _parse_iso(value: Any) -> Optional[datetime]:
    s = clean_str(value)
    if s is None:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)  # ValueError if not ISO 8601


def load_zone(name: Optional[str], path: str, warn: Warn) -> Optional[ZoneInfo]:
    if name is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        warn(f"{path}: unknown time zone {name!r}")
        return None


def parse_local_time(value: Any, tz: Optional[ZoneInfo], path: str, warn: Warn) -> Optional[When]:
    """aviationstack time -> When. Any offset it carries is ignored (it is fake)."""
    try:
        dt = _parse_iso(value)
    except (ValueError, TypeError):
        warn(f"{path}: date with unexpected format {value!r}")
        return None
    if dt is None:
        return None
    local = dt.replace(tzinfo=None)
    if tz is None:
        return When(local=local, utc=None)

    first = local.replace(tzinfo=tz, fold=0)
    second = local.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset():
        roundtrip = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        kind = "nonexistent" if roundtrip != local else "ambiguous"
        warn(f"{path}: {kind} local time due to a DST change ({local.isoformat()}), using the first interpretation")
    return When(local=local, utc=first.astimezone(timezone.utc))


def parse_utc_time(value: Any, path: str, warn: Warn) -> Optional[datetime]:
    try:
        dt = _parse_iso(value)
    except (ValueError, TypeError):
        warn(f"{path}: date with unexpected format {value!r}")
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_status(value: Any, warn: Warn) -> FlightStatus:
    s = clean_str(value)
    if s is None:
        return FlightStatus.UNKNOWN
    try:
        return FlightStatus(s.lower())
    except ValueError:
        warn(f"flight_status: unknown value {value!r}")
        return FlightStatus.UNKNOWN


# --------------------------------------------------------------------------
# Record -> Flight
# --------------------------------------------------------------------------
def _endpoint(raw: Dict[str, Any], side: str, warn: Warn, required: bool) -> Endpoint:
    iata = clean_code(raw.get("iata"), AIRPORT_IATA, f"{side}.iata", warn)
    if iata is None:
        raise RecordError(f"{side}.iata missing or invalid ({raw.get('iata')!r})")
    tz_name = clean_str(raw.get("timezone"))
    if tz_name is None:
        warn(f"{side}.timezone missing: local times only, no UTC")
    tz = load_zone(tz_name, f"{side}.timezone", warn)

    times = {k: parse_local_time(raw.get(k), tz, f"{side}.{k}", warn) for k in ("scheduled", "estimated", "actual")}
    if required and times["scheduled"] is None:
        raise RecordError(f"{side}.scheduled missing or invalid ({raw.get('scheduled')!r})")

    delay = parse_int(raw.get("delay"), f"{side}.delay", warn)
    if delay is not None and abs(delay) > 7 * 24 * 60:
        warn(f"{side}.delay out of range ({delay} min), discarded")
        delay = None

    return Endpoint(
        airport=Airport(
            iata=iata,
            icao=clean_code(raw.get("icao"), AIRPORT_ICAO, f"{side}.icao", warn),
            name=clean_name(raw.get("airport")),
            timezone=tz_name if tz else None,
        ),
        scheduled=times["scheduled"],
        estimated=times["estimated"],
        actual=times["actual"],
        terminal=clean_str(raw.get("terminal")),
        gate=clean_str(raw.get("gate")),
        baggage=clean_str(raw.get("baggage")),
        delay_min=delay,
    )


def _live(raw: Any, warn: Warn) -> Optional[LivePosition]:
    if not isinstance(raw, dict):
        return None
    lat = parse_float(raw.get("latitude"), "live.latitude", warn)
    lon = parse_float(raw.get("longitude"), "live.longitude", warn)
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        warn(f"live: invalid coordinates ({raw.get('latitude')!r}, {raw.get('longitude')!r}), discarded")
        return None
    is_ground = raw.get("is_ground")
    return LivePosition(
        updated=parse_utc_time(raw.get("updated"), "live.updated", warn),
        latitude=lat,
        longitude=lon,
        altitude=parse_float(raw.get("altitude"), "live.altitude", warn),
        direction=parse_float(raw.get("direction"), "live.direction", warn),
        speed_horizontal=parse_float(raw.get("speed_horizontal"), "live.speed_horizontal", warn),
        speed_vertical=parse_float(raw.get("speed_vertical"), "live.speed_vertical", warn),
        is_ground=is_ground if isinstance(is_ground, bool) else None,
    )


def parse_flight(raw: Any) -> Tuple[Flight, Optional[Dict[str, Any]]]:
    """Return (Flight, operating-flight data if it is a codeshare).

    Raises RecordError if the minimum data is missing: departure and arrival
    airports and the scheduled arrival time.
    """
    if not isinstance(raw, dict):
        raise RecordError(f"the record is not a JSON object ({type(raw).__name__})")
    warnings: List[str] = []
    warn = warnings.append

    for side in ("departure", "arrival"):
        if not isinstance(raw.get(side), dict):
            raise RecordError(f"{side} missing or of unexpected type ({type(raw.get(side)).__name__})")
    arrival = _endpoint(raw["arrival"], "arrival", warn, required=True)
    departure = _endpoint(raw["departure"], "departure", warn, required=False)

    flight_date: Optional[date] = None
    if raw.get("flight_date") is not None:
        try:
            flight_date = date.fromisoformat(str(raw["flight_date"]).strip())
        except ValueError:
            warn(f"flight_date: unexpected format {raw['flight_date']!r}")
    if flight_date is None:
        base = departure.scheduled or arrival.scheduled
        flight_date = base.local.date() if base else None
        warn("flight_date missing: using the scheduled local date")

    airline_raw, flight_raw, aircraft_raw = _dict(raw.get("airline")), _dict(raw.get("flight")), _dict(raw.get("aircraft"))

    dep_s, arr_s = departure.scheduled, arrival.scheduled
    if dep_s and arr_s and dep_s.utc and arr_s.utc:
        hours = (arr_s.utc - dep_s.utc).total_seconds() / 3600
        if hours <= 0:
            warn(f"scheduled arrival before departure ({hours:+.1f} h)")
        elif hours > 24:
            warn(f"unusual scheduled duration ({hours:.1f} h)")

    flight = Flight(
        flight_date=flight_date,
        status=parse_status(raw.get("flight_status"), warn),
        airline=Airline(
            name=clean_name(airline_raw.get("name")),
            iata=clean_code(airline_raw.get("iata"), AIRLINE_IATA, "airline.iata", warn),
            icao=clean_code(airline_raw.get("icao"), AIRLINE_ICAO, "airline.icao", warn),
        ),
        flight_number=clean_code(flight_raw.get("number"), FLIGHT_NUMBER, "flight.number", warn),
        flight_iata=clean_code(flight_raw.get("iata"), FLIGHT_CODE, "flight.iata", warn),
        flight_icao=clean_code(flight_raw.get("icao"), FLIGHT_CODE, "flight.icao", warn),
        departure=departure,
        arrival=arrival,
        aircraft_registration=clean_str(aircraft_raw.get("registration")),
        aircraft_type=clean_str(aircraft_raw.get("iata")),
        live=_live(raw.get("live"), warn),
        warnings=tuple(warnings),
    )
    codeshared = flight_raw.get("codeshared")
    return flight, codeshared if isinstance(codeshared, dict) else None


def _operator_from_codeshare(marketing: Flight, cs: Dict[str, Any]) -> Optional[Flight]:
    """Build the operating flight when only the codeshare record arrived."""
    warnings: List[str] = list(marketing.warnings)
    warn = warnings.append
    iata = clean_code(cs.get("flight_iata"), FLIGHT_CODE, "codeshared.flight_iata", warn)
    if iata is None:
        return None
    warn("operating flight rebuilt from a codeshare")
    return replace(
        marketing,
        airline=Airline(
            name=clean_name(cs.get("airline_name")),
            iata=clean_code(cs.get("airline_iata"), AIRLINE_IATA, "codeshared.airline_iata", warn),
            icao=clean_code(cs.get("airline_icao"), AIRLINE_ICAO, "codeshared.airline_icao", warn),
        ),
        flight_number=clean_code(cs.get("flight_number"), FLIGHT_NUMBER, "codeshared.flight_number", warn),
        flight_iata=iata,
        flight_icao=clean_code(cs.get("flight_icao"), FLIGHT_CODE, "codeshared.flight_icao", warn),
        codeshares=(),
        warnings=tuple(warnings),
    )


def _sort_key(f: Flight):
    w = f.arrival.scheduled
    if w is None:
        return (2, datetime.max)
    if w.utc is not None:
        return (0, w.utc.replace(tzinfo=None))
    return (1, w.local)


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------
def normalize_flights(raws: Iterable[Any], dedupe: bool = True) -> NormalizeResult:
    """Normalize every record. A bad record is rejected; it never breaks the batch."""
    result = NormalizeResult()
    parsed: List[Tuple[Flight, Optional[Dict[str, Any]]]] = []

    for i, raw in enumerate(raws):
        try:
            parsed.append(parse_flight(raw))
        except RecordError as e:
            result.rejected.append(Rejected(i, str(e), raw))
            log.warning("Record %d rejected: %s", i, e)
        except Exception as e:  # unexpected data shape: reject, don't crash
            reason = f"unexpected error: {e.__class__.__name__}: {e}"
            result.rejected.append(Rejected(i, reason, raw))
            log.warning("Record %d rejected (%s)", i, reason, exc_info=log.isEnabledFor(logging.DEBUG))

    if not dedupe:
        result.flights = [f for f, _ in parsed]
        return result

    by_key: Dict[Tuple[str, Optional[date]], Flight] = {}
    standalone: List[Flight] = []

    # 1) Operating flights (not codeshares).
    for flight, cs in parsed:
        if cs is not None:
            continue
        if flight.flight_iata is None:
            standalone.append(flight)
            continue
        key = (flight.flight_iata, flight.flight_date)
        if key in by_key:
            result.codeshares_merged += 1  # exact duplicate of the same flight
            continue
        by_key[key] = flight

    # 2) Codeshares: attach to their operator (or create it if it didn't arrive).
    for flight, cs in parsed:
        if cs is None:
            continue
        op_iata = clean_str(cs.get("flight_iata"))
        key = (op_iata.upper() if op_iata else "", flight.flight_date)
        operator = by_key.get(key)
        if operator is None:
            operator = _operator_from_codeshare(flight, cs)
            if operator is None:
                standalone.append(flight)
                continue
        marketing = flight.flight_iata
        if marketing and marketing not in operator.codeshares and marketing != operator.flight_iata:
            operator = replace(operator, codeshares=operator.codeshares + (marketing,))
        by_key[key] = operator
        result.codeshares_merged += 1

    result.flights = sorted(list(by_key.values()) + standalone, key=_sort_key)
    return result
