"""Our own data model, independent of the aviationstack format.

Conventions:
  * Times are stored as `When`: the airport's local wall-clock time
    (`local`, naive) + the UTC instant (`utc`, aware) when the time zone is
    known. If the zone is missing, `utc` is None: it is never guessed.
  * Missing optional fields are None.
  * `warnings` collects the record's data-quality issues.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class FlightStatus(str, Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    LANDED = "landed"
    CANCELLED = "cancelled"
    INCIDENT = "incident"
    DIVERTED = "diverted"
    UNKNOWN = "unknown"


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class When:
    local: datetime                 # airport local time, no zone
    utc: Optional[datetime] = None  # real instant; None if the zone is unknown

    def to_dict(self) -> Dict[str, Any]:
        return {"local": self.local.isoformat(), "utc": _iso_utc(self.utc)}


@dataclass(frozen=True)
class Airport:
    iata: str
    icao: Optional[str] = None
    name: Optional[str] = None
    timezone: Optional[str] = None


@dataclass(frozen=True)
class Endpoint:
    airport: Airport
    scheduled: Optional[When] = None
    estimated: Optional[When] = None
    actual: Optional[When] = None
    terminal: Optional[str] = None
    gate: Optional[str] = None
    baggage: Optional[str] = None
    delay_min: Optional[int] = None

    @property
    def best(self) -> Optional[When]:
        """Best available time: actual > estimated > scheduled."""
        return self.actual or self.estimated or self.scheduled


@dataclass(frozen=True)
class Airline:
    name: Optional[str] = None
    iata: Optional[str] = None
    icao: Optional[str] = None


@dataclass(frozen=True)
class LivePosition:
    updated: Optional[datetime]
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    direction: Optional[float] = None
    speed_horizontal: Optional[float] = None
    speed_vertical: Optional[float] = None
    is_ground: Optional[bool] = None


@dataclass(frozen=True)
class Flight:
    flight_date: Optional[date]
    status: FlightStatus
    airline: Airline
    flight_number: Optional[str]
    flight_iata: Optional[str]
    flight_icao: Optional[str]
    departure: Endpoint
    arrival: Endpoint
    codeshares: Tuple[str, ...] = ()
    aircraft_registration: Optional[str] = None
    aircraft_type: Optional[str] = None
    live: Optional[LivePosition] = None
    warnings: Tuple[str, ...] = field(default=())

    @property
    def natural_key(self) -> str:
        """Stable identity of the physical flight, used to deduplicate in the DB.

        * With a flight code: "UX51|2026-11-15|MAD"
        * Without one: "MAD-HAV|dep=2026-11-15T15:05" (scheduled local time;
          arrival time if the departure time is missing).
        """
        dep, arr = self.departure.airport.iata, self.arrival.airport.iata
        if self.flight_iata:
            day = self.flight_date.isoformat() if self.flight_date else "?"
            return f"{self.flight_iata}|{day}|{dep}"
        if self.departure.scheduled:
            return f"{dep}-{arr}|dep={self.departure.scheduled.local.isoformat(timespec='minutes')}"
        if self.arrival.scheduled:
            return f"{dep}-{arr}|arr={self.arrival.scheduled.local.isoformat(timespec='minutes')}"
        raise ValueError("flight has neither a code nor a scheduled time")

    def to_dict(self) -> Dict[str, Any]:
        def when(w: Optional[When]):
            return w.to_dict() if w else None

        def endpoint(e: Endpoint):
            return {
                "iata": e.airport.iata,
                "icao": e.airport.icao,
                "airport": e.airport.name,
                "timezone": e.airport.timezone,
                "scheduled": when(e.scheduled),
                "estimated": when(e.estimated),
                "actual": when(e.actual),
                "terminal": e.terminal,
                "gate": e.gate,
                "baggage": e.baggage,
                "delay_min": e.delay_min,
            }

        live = None
        if self.live:
            live = {
                "updated": _iso_utc(self.live.updated),
                "latitude": self.live.latitude,
                "longitude": self.live.longitude,
                "altitude": self.live.altitude,
                "direction": self.live.direction,
                "speed_horizontal": self.live.speed_horizontal,
                "speed_vertical": self.live.speed_vertical,
                "is_ground": self.live.is_ground,
            }
        return {
            "flight_date": self.flight_date.isoformat() if self.flight_date else None,
            "status": self.status.value,
            "airline": {"name": self.airline.name, "iata": self.airline.iata, "icao": self.airline.icao},
            "flight_number": self.flight_number,
            "flight_iata": self.flight_iata,
            "flight_icao": self.flight_icao,
            "codeshares": list(self.codeshares),
            "departure": endpoint(self.departure),
            "arrival": endpoint(self.arrival),
            "aircraft": {"registration": self.aircraft_registration, "type": self.aircraft_type},
            "live": live,
            "warnings": list(self.warnings),
        }
