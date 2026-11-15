"""Import flights into PostgreSQL.

    python -m flights_to_cuba.importer --from-file flights.json   # 0 API requests
    python -m flights_to_cuba.importer                            # 1 API request

Safe to run any number of times: see `repository.py` for the idempotency rules.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from .config import load_dotenv
from .client import AviationstackClient
from .parser import normalize_flights
from .repository import FlightRepository, connect
from .service import fetch_and_store


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import flights arriving in Cuba into PostgreSQL")
    p.add_argument("--airport", default="HAV", help="Arrival IATA code (API mode)")
    p.add_argument("--from-file", help="Import a saved raw API response instead of calling the API")
    p.add_argument("--save-raw", help="When calling the API, also save the raw response here")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(Path.cwd() / ".env")

    try:
        with connect() as conn:
            repo = FlightRepository(conn)
            repo.migrate()
            if args.from_file:
                path = Path(args.from_file)
                raw = json.loads(path.read_text())
                if not isinstance(raw, list):
                    print("Error: the file must contain a JSON list of flight records", file=sys.stderr)
                    return 1
                # The file's modification time is when it was downloaded.
                fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                summary = repo.import_result(raw, normalize_flights(raw), source=f"file:{path.name}",
                                             fetched_at=fetched_at)
            else:
                save = None
                if args.save_raw:
                    def save(raw):
                        Path(args.save_raw).write_text(json.dumps(raw, indent=2, ensure_ascii=False))
                summary, error = fetch_and_store(repo, lambda: AviationstackClient(max_requests=3),
                                                 args.airport, save_raw=save)
                if error:
                    print(f"Error: {error}", file=sys.stderr)
                    return 1
            total = repo.count_flights()
    except (psycopg.Error, RuntimeError) as e:  # connection refused, bad credentials, no DATABASE_URL
        print(f"Database error: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1

    print(f"{summary} · {total} flights in the database")
    return 0


if __name__ == "__main__":
    sys.exit(main())
