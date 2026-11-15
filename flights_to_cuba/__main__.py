"""Preview from the command line: fetch flights, parse them and print a table (no database).

    python -m flights_to_cuba --arr HAV --out flights.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .client import AviationstackClient, AviationstackError, RequestBudgetExceeded, RetriesExhausted
from .config import load_dotenv
from .parser import normalize_flights


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Flights arriving in Cuba, via aviationstack")
    p.add_argument("--arr", default="HAV", help="Arrival IATA code (default HAV)")
    p.add_argument("--dep", help="Optional departure IATA code (e.g. MIA)")
    p.add_argument("--status", help="scheduled, active, landed, cancelled...")
    p.add_argument("--max-pages", type=int, default=1, help="Max pages (each one = 1 request)")
    p.add_argument("--max-requests", type=int, default=10, help="Request cap per run")
    p.add_argument("--out", help="Save the clean data model to this JSON file")
    p.add_argument("--raw-out", help="Save the raw API response to this JSON file")
    p.add_argument("--no-dedupe", action="store_true", help="Don't group codeshares under the operating flight")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    load_dotenv(Path.cwd() / ".env")

    filters = {k: v for k, v in {"dep_iata": args.dep, "flight_status": args.status}.items() if v}
    try:
        client = AviationstackClient(max_requests=args.max_requests)
        raw = client.flights_to(args.arr, max_pages=args.max_pages, **filters)
    except ValueError as e:
        print(f"Configuration: {e}", file=sys.stderr)
        return 2
    except (AviationstackError, RetriesExhausted, RequestBudgetExceeded) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    result = normalize_flights(raw, dedupe=not args.no_dedupe)
    for f in result.flights:
        when = f.arrival.best
        hhmm = when.local.strftime("%d/%m %H:%M") if when else "?"
        codeshares = f" (+{', '.join(f.codeshares)})" if f.codeshares else ""
        flag = " ⚠" if f.warnings else ""
        print(f"{hhmm}  {f.flight_iata or '?':<8} {f.airline.name or '?':<26} "
              f"{f.departure.airport.iata} → {f.arrival.airport.iata}  {f.status.value:<10}{codeshares}{flag}")
    sys.stdout.flush()

    print(f"\n{len(raw)} records → {len(result.flights)} flights "
          f"({result.codeshares_merged} codeshares/duplicates grouped, "
          f"{len(result.rejected)} rejected, {result.with_warnings} with warnings) · "
          f"{client.requests_made} API request(s). Times are local to the arrival airport.",
          file=sys.stderr)

    if args.out:
        payload = {"flights": [f.to_dict() for f in result.flights],
                   "rejected": [r.to_dict() for r in result.rejected]}
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    if args.raw_out:
        Path(args.raw_out).write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
