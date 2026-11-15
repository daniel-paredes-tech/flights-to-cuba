-- Schema for flights-to-cuba (PostgreSQL 15+).
-- Every statement is idempotent: applying this file twice is a no-op.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    CREATE TYPE flight_status AS ENUM
        ('scheduled', 'active', 'landed', 'cancelled', 'incident', 'diverted', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Airports, keyed by their IATA code.
CREATE TABLE IF NOT EXISTS airports (
    iata      char(3) PRIMARY KEY CHECK (iata ~ '^[A-Z]{3}$'),
    icao      char(4) CHECK (icao ~ '^[A-Z0-9]{4}$'),
    name      text,
    timezone  text     -- IANA zone, e.g. 'America/Havana'
);

-- Airlines. Codes can be missing, so a surrogate key is used. Airlines with
-- codes are unique by (iata, icao) (NULLs compared as equal); airlines with
-- no codes at all are unique by name.
CREATE TABLE IF NOT EXISTS airlines (
    id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    iata  varchar(2) CHECK (iata ~ '^[A-Z0-9]{2}$'),
    icao  char(3)    CHECK (icao ~ '^[A-Z]{3}$'),
    name  text,
    CONSTRAINT airlines_has_identity CHECK (iata IS NOT NULL OR icao IS NOT NULL OR name IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS airlines_codes_uidx ON airlines (iata, icao) NULLS NOT DISTINCT
    WHERE iata IS NOT NULL OR icao IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS airlines_name_only_uidx ON airlines (name)
    WHERE iata IS NULL AND icao IS NULL;

-- Each fetch/import of the API response. The SHA-256 of the payload makes
-- re-importing the very same response a no-op.
CREATE TABLE IF NOT EXISTS import_batches (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payload_sha256     char(64) NOT NULL UNIQUE,
    source             text NOT NULL,
    fetched_at         timestamptz NOT NULL,
    imported_at        timestamptz NOT NULL DEFAULT now(),
    record_count       integer NOT NULL CHECK (record_count >= 0),
    flight_count       integer NOT NULL DEFAULT 0,
    inserted_count     integer NOT NULL DEFAULT 0,
    updated_count      integer NOT NULL DEFAULT 0,
    unchanged_count    integer NOT NULL DEFAULT 0,
    rejected_count     integer NOT NULL DEFAULT 0,
    payload            jsonb NOT NULL
);

-- One row per physical (operating) flight. `natural_key` is what makes
-- the import idempotent: the same flight always maps to the same row.
CREATE TABLE IF NOT EXISTS flights (
    id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    natural_key               text NOT NULL UNIQUE,
    flight_date               date,
    status                    flight_status NOT NULL DEFAULT 'unknown',
    airline_id                integer REFERENCES airlines (id),
    flight_number             varchar(6),
    flight_iata               varchar(9),
    flight_icao               varchar(9),

    departure_iata            char(3) NOT NULL REFERENCES airports (iata),
    departure_scheduled_local timestamp,
    departure_scheduled_utc   timestamptz,
    departure_estimated_local timestamp,
    departure_estimated_utc   timestamptz,
    departure_actual_local    timestamp,
    departure_actual_utc      timestamptz,
    departure_terminal        text,
    departure_gate            text,
    departure_delay_min       integer,

    arrival_iata              char(3) NOT NULL REFERENCES airports (iata),
    arrival_scheduled_local   timestamp NOT NULL,
    arrival_scheduled_utc     timestamptz,
    arrival_estimated_local   timestamp,
    arrival_estimated_utc     timestamptz,
    arrival_actual_local      timestamp,
    arrival_actual_utc        timestamptz,
    arrival_terminal          text,
    arrival_gate              text,
    arrival_baggage           text,
    arrival_delay_min         integer,

    aircraft_registration     text,
    aircraft_type             text,
    warnings                  text[] NOT NULL DEFAULT '{}',

    source_fetched_at         timestamptz NOT NULL,  -- when the API data was fetched
    last_batch_id             bigint REFERENCES import_batches (id),
    first_seen_at             timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS flights_arrival_idx ON flights (arrival_iata, arrival_scheduled_utc);
CREATE INDEX IF NOT EXISTS flights_date_idx ON flights (flight_date);

-- Other airlines' codes sold on the same flight.
CREATE TABLE IF NOT EXISTS flight_codeshares (
    flight_id    bigint NOT NULL REFERENCES flights (id) ON DELETE CASCADE,
    flight_iata  varchar(9) NOT NULL,
    PRIMARY KEY (flight_id, flight_iata)
);

-- Position snapshots; one per flight and timestamp.
CREATE TABLE IF NOT EXISTS live_positions (
    flight_id         bigint NOT NULL REFERENCES flights (id) ON DELETE CASCADE,
    observed_at       timestamptz NOT NULL,
    latitude          double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude         double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    altitude          double precision,
    direction         double precision,
    speed_horizontal  double precision,
    speed_vertical    double precision,
    is_ground         boolean,
    PRIMARY KEY (flight_id, observed_at)
);

-- Records the normalizer rejected, kept for inspection.
CREATE TABLE IF NOT EXISTS rejected_records (
    batch_id      bigint NOT NULL REFERENCES import_batches (id) ON DELETE CASCADE,
    record_index  integer NOT NULL,
    reason        text NOT NULL,
    raw           jsonb,
    PRIMARY KEY (batch_id, record_index)
);

-- Every call to the API (successful or not). Used to decide when the API may
-- be called again, so failures back off instead of draining the quota.
CREATE TABLE IF NOT EXISTS api_fetch_attempts (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    airport       char(3) NOT NULL,
    attempted_at  timestamptz NOT NULL,
    success       boolean NOT NULL,
    error         text,
    batch_id      bigint REFERENCES import_batches (id) ON DELETE SET NULL,
    CHECK (success OR error IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS api_fetch_attempts_airport_idx ON api_fetch_attempts (airport, attempted_at DESC);

INSERT INTO schema_migrations (version) VALUES (1), (2) ON CONFLICT DO NOTHING;
