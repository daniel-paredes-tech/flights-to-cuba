# flights-to-cuba

Imports flights arriving in Havana (HAV) from the aviationstack API into a local PostgreSQL database.

## The API: aviationstack, and why

I wanted to follow the flights that arrive in Cuba, so I needed a flight-data API that can filter by destination. [aviationstack](https://aviationstack.com/) fits well:

- **Free plan:** 100 requests per month, enough for this project.
- **Filter by arrival airport:** one request (`/v1/flights?arr_iata=HAV`) returns the current arrivals in Havana.
- **Real-time data in plain JSON:** flight status, scheduled, estimated and actual times, airline, origin, codeshares and live positions.

## What's in the repo

| Part | File | What it does |
|---|---|---|
| Client | `flights_to_cuba/client.py` | Calls the API with timeouts, retries with backoff, and rate limiting |
| Parser | `flights_to_cuba/parser.py`, `models.py` | Turns raw records into clean `Flight` objects; a bad record is rejected without stopping the run |
| Repository | `flights_to_cuba/repository.py` | The only code that reads or writes the database |
| Importer | `flights_to_cuba/importer.py` | Command that fetches, parses and stores the data |
| Database | `docker-compose.yml`, `flights_to_cuba/db/schema.sql` | PostgreSQL 18 in Docker and the schema |
| Demo queries | `queries/demo.sql` | Queries to explore what's stored |
| Tests | `tests/` | Unit tests, plus database tests against a throwaway database |

Extra: `python -m flights_to_cuba.server` serves a private page at http://localhost:8000 that shows the stored flights.

## How to run it

Requirements: Python 3.9+, Docker, and an aviationstack API key.

```bash
# 1. Install and configure
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then add your API key and a database password

# 2. Start the database
docker compose up -d

# 3. Run the import (uses 1 API request; the schema is created automatically)
.venv/bin/python -m flights_to_cuba.importer

# Or import a saved API response without using a request
.venv/bin/python -m flights_to_cuba.importer --from-file flights.json

# 4. Look at the data
docker compose exec -T db psql -U flights -d flights < queries/demo.sql

# Tests
.venv/bin/python -m unittest
```

The database listens on `127.0.0.1:5432` only, so it can't be reached from other devices.

## Database

| Table | Key | Contents |
|---|---|---|
| `flights` | `id`, unique `natural_key` | One row per physical flight: status, times (local and UTC), terminals, gates, delays, data-quality warnings |
| `airports` | `iata` | Name, ICAO code, time zone |
| `airlines` | `id`, unique codes | Name, IATA and ICAO codes |
| `flight_codeshares` | `(flight_id, flight_iata)` | Other airlines' flight numbers for the same flight |
| `live_positions` | `(flight_id, observed_at)` | Position snapshots |
| `import_batches` | `id`, unique `payload_sha256` | Each import: raw response and counts |
| `rejected_records` | `(batch_id, record_index)` | Records the parser rejected, with the reason |
| `api_fetch_attempts` | `id` | Every API call, successful or not |

**Running the import twice never creates duplicates:**

- The same API response is recognised by its SHA-256 hash and skipped.
- A flight always maps to the same row through `natural_key` (e.g. `UX51|2026-11-15|MAD`). Re-imports use `INSERT … ON CONFLICT DO UPDATE` and only write when a value really changed. Older data never overwrites newer data.
- Each import runs in one transaction: it is stored completely or not at all.

## Surprises in the data

From a sample of 81 records for arrivals in Havana:

- **The times say UTC, but they aren't.** Every time ends in `+00:00`, but it is the airport's local time. Read as UTC, Madrid → Havana would take 5 hours instead of 11. The parser drops the fake offset and converts with each airport's time zone, storing both local time and UTC.
- **One aircraft appears up to five times.** Codeshares come as separate records (Copa CM217 is also sold as Turkish, KLM, Iberia and Air France flights). The 81 records are 67 physical flights; the parser groups codeshares under the operating flight.
- **Missing values come in several shapes.** Usually `null`, but 4 records have the airline name set to the string `"empty"` and no flight number.
- **Status is often missing.** 19 of 81 records have no `flight_status`, including flights that have already landed. They are stored as `unknown`.
- **Some airports have no time zone.** 3 records had no departure time zone, so only their local time could be stored. Their UTC time is left empty instead of being guessed.
- **Codes aren't consistent.** Codeshare data uses lowercase codes (`ux51`) while the rest uses uppercase. The parser uppercases and validates every code.
