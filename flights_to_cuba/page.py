"""Renders the flights stored in PostgreSQL as an HTML page."""

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _when(local: Optional[datetime], utc: Optional[datetime]) -> Optional[Dict[str, Any]]:
    if local is None:
        return None
    return {"local": local.isoformat(), "utc": iso(utc) if utc else None}


def flight_from_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """A `flights` row (from FlightRepository.list_flights) as a JSON-ready dict."""
    def side(prefix: str) -> Dict[str, Any]:
        return {
            "iata": r[f"{prefix}_iata"],
            "airport": r.get(f"{prefix}_airport_name"),
            "scheduled": _when(r[f"{prefix}_scheduled_local"], r[f"{prefix}_scheduled_utc"]),
            "estimated": _when(r[f"{prefix}_estimated_local"], r[f"{prefix}_estimated_utc"]),
            "actual": _when(r[f"{prefix}_actual_local"], r[f"{prefix}_actual_utc"]),
            "terminal": r[f"{prefix}_terminal"],
            "gate": r[f"{prefix}_gate"],
            "delay_min": r[f"{prefix}_delay_min"],
        }

    arrival = side("arrival")
    arrival["baggage"] = r["arrival_baggage"]
    return {
        "natural_key": r["natural_key"],
        "flight_date": r["flight_date"].isoformat() if r["flight_date"] else None,
        "status": str(r["status"]),
        "airline": {"name": r.get("airline_name"), "iata": r.get("airline_iata"), "icao": r.get("airline_icao")},
        "flight_number": r["flight_number"],
        "flight_iata": r["flight_iata"],
        "flight_icao": r["flight_icao"],
        "codeshares": list(r.get("codeshares") or []),
        "departure": side("departure"),
        "arrival": arrival,
        "aircraft": {"registration": r["aircraft_registration"], "type": r["aircraft_type"]},
        "warnings": list(r["warnings"] or []),
        "updated_at": iso(r["updated_at"]),
    }


# --------------------------------------------------------------------- HTML
def _fmt_local(when: Any) -> str:
    if not isinstance(when, dict):
        return ""
    dt = parse_iso(when.get("local")) if when.get("local") else None
    return dt.strftime("%a %d %b %H:%M") if dt else ""


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min ago"


def _row(f: Dict[str, Any]) -> str:
    e = html.escape
    arr = f.get("arrival") or {}
    dep = f.get("departure") or {}
    airline = f.get("airline") or {}
    best_key = next((k for k in ("actual", "estimated", "scheduled") if arr.get(k)), None)
    best = _fmt_local(arr.get(best_key)) if best_key else ""
    sched = _fmt_local(arr.get("scheduled"))
    time_note = "" if best_key in (None, "scheduled") or best == sched else f'<div class="sub">sched. {e(sched)}</div>'
    status = str(f.get("status") or "unknown")
    codeshares = ", ".join(str(c) for c in f.get("codeshares") or [])
    return (
        "<tr>"
        f'<td class="time">{e(best or "—")}<div class="sub">{e(best_key or "")}</div>{time_note}</td>'
        f'<td class="flight">{e(f.get("flight_iata") or "—")}</td>'
        f'<td>{e(airline.get("name") or "—")}</td>'
        f'<td><strong>{e(dep.get("iata") or "?")}</strong><div class="sub">{e(dep.get("airport") or "")}</div></td>'
        f'<td><span class="status status-{e(status)}">{e(status)}</span></td>'
        f'<td class="sub">{e(codeshares)}</td>'
        "</tr>"
    )


def render_html(
    payload: Dict[str, Any],
    now: Optional[datetime] = None,
    next_refresh_at: Optional[datetime] = None,
    message: Optional[str] = None,
) -> str:
    """`next_refresh_at`: None = manual refresh available now."""
    e = html.escape
    now = now or utc_now()
    airport = str(payload.get("airport") or "HAV")
    flights = payload.get("flights") or []
    generated = parse_iso(payload.get("generated_at"))
    updated = f"updated {_ago(now - generated)} ({generated:%d %b %H:%M} UTC)" if generated else "never updated"

    notices = []
    if message:
        notices.append(f'<p class="notice">{e(message)}</p>')
    if payload.get("stale"):
        notices.append('<p class="notice warn">The last API call failed; showing the most recent data available. '
                       f'({e(str(payload.get("stale_reason") or ""))})</p>')

    if next_refresh_at is None:
        refresh = '<form method="post" action="/refresh"><button type="submit">Refresh now (1 API request)</button></form>'
    else:
        refresh = (f'<button type="button" disabled>Refresh available at {next_refresh_at:%d %b %H:%M} UTC</button>')

    rows = "\n".join(_row(f) for f in flights) or '<tr><td colspan="6" class="empty">No flights available.</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flights to Cuba</title>
<style>
:root {{
  --bg: #f7f7f5; --surface: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --border: #e3e3de;
  --accent: #0b6e4f; --accent-text: #ffffff; --note-bg: #e8f1ec; --warn-bg: #fff4d6; --warn-text: #6b4e00;
  --s-active: #0b6e4f; --s-landed: #3a5a8c; --s-scheduled: #6b6b66; --s-cancelled: #b3261e;
  --s-diverted: #a35200; --s-incident: #b3261e; --s-unknown: #8a8a84;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #141413; --surface: #1d1d1b; --text: #ecebe6; --muted: #a3a29b; --border: #2f2f2c;
    --accent: #4cc29a; --accent-text: #0d1f18; --note-bg: #1c2d26; --warn-bg: #3a2f10; --warn-text: #f3d48a;
    --s-active: #4cc29a; --s-landed: #8fb0e6; --s-scheduled: #a3a29b; --s-cancelled: #f28b82;
    --s-diverted: #f2b36b; --s-incident: #f28b82; --s-unknown: #8a8a84;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 1040px; margin: 0 auto; padding: 32px 16px 48px; }}
h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
.bar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin: 0 0 20px; }}
.meta {{ color: var(--muted); margin: 0; }}
.meta a {{ color: var(--accent); }}
form {{ margin: 0; }}
button {{ font: inherit; padding: 8px 14px; border-radius: 8px; border: 1px solid var(--accent);
  background: var(--accent); color: var(--accent-text); cursor: pointer; }}
button:disabled {{ background: transparent; color: var(--muted); border-color: var(--border); cursor: default; }}
.notice {{ background: var(--note-bg); padding: 10px 14px; border-radius: 8px; }}
.notice.warn {{ background: var(--warn-bg); color: var(--warn-text); }}
.table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }}
table {{ width: 100%; border-collapse: collapse; min-width: 640px; }}
th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
th {{ font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }}
tr:last-child td {{ border-bottom: 0; }}
.time {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
.flight {{ font-weight: 600; white-space: nowrap; }}
.sub {{ color: var(--muted); font-size: .82rem; }}
.status {{ font-weight: 600; text-transform: capitalize; }}
.status-active {{ color: var(--s-active); }} .status-landed {{ color: var(--s-landed); }}
.status-scheduled {{ color: var(--s-scheduled); }} .status-cancelled {{ color: var(--s-cancelled); }}
.status-diverted {{ color: var(--s-diverted); }} .status-incident {{ color: var(--s-incident); }}
.status-unknown {{ color: var(--s-unknown); }}
.empty {{ text-align: center; color: var(--muted); padding: 32px; }}
footer {{ color: var(--muted); font-size: .82rem; margin-top: 16px; }}
</style>
</head>
<body>
<main>
<h1>Flights to Cuba — arrivals at {e(airport)}</h1>
<div class="bar">
<p class="meta">{len(flights)} flights · {e(updated)} · local Havana times · <a href="/flights.json">JSON</a></p>
{refresh}
</div>
{"".join(notices)}
<div class="table-wrap">
<table>
<thead><tr><th>Arrival</th><th>Flight</th><th>Airline</th><th>From</th><th>Status</th><th>Also sold as</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<footer>Data from aviationstack, stored in PostgreSQL. Refreshed automatically when older than 12 hours; codeshares are grouped under the operating flight.</footer>
</main>
</body>
</html>
"""
