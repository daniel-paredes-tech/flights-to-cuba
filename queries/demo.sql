-- Queries to explore the database.
-- Run them all:  docker compose exec -T db psql -U flights -d flights < queries/demo.sql
-- Or open a shell and paste one at a time:  docker compose exec db psql -U flights -d flights

-- 1. How many rows each table holds.
SELECT 'flights' AS table_name, count(*) FROM flights
UNION ALL SELECT 'airports', count(*) FROM airports
UNION ALL SELECT 'airlines', count(*) FROM airlines
UNION ALL SELECT 'flight_codeshares', count(*) FROM flight_codeshares
UNION ALL SELECT 'live_positions', count(*) FROM live_positions
UNION ALL SELECT 'import_batches', count(*) FROM import_batches
UNION ALL SELECT 'rejected_records', count(*) FROM rejected_records;

-- 2. Import history: what each run added or changed.
SELECT id, source, fetched_at, record_count, flight_count,
       inserted_count, updated_count, unchanged_count, rejected_count
FROM import_batches
ORDER BY id;

-- 3. Proof of idempotency: no flight is stored twice (expect 0 rows).
SELECT natural_key, count(*)
FROM flights
GROUP BY natural_key
HAVING count(*) > 1;

-- 4. Arrivals in Havana, in local time, with their status.
SELECT to_char(f.arrival_scheduled_local, 'Dy DD Mon HH24:MI') AS arrives,
       f.flight_iata, a.name AS airline, f.departure_iata AS origin, f.status
FROM flights f
LEFT JOIN airlines a ON a.id = f.airline_id
WHERE f.arrival_iata = 'HAV'
ORDER BY f.arrival_scheduled_utc NULLS LAST;

-- 5. Where the flights come from.
SELECT f.departure_iata AS origin, ap.name AS airport, count(*) AS flights
FROM flights f
JOIN airports ap ON ap.iata = f.departure_iata
GROUP BY f.departure_iata, ap.name
ORDER BY flights DESC;

-- 6. Busiest airlines.
SELECT coalesce(a.name, '(unknown)') AS airline, count(*) AS flights
FROM flights f
LEFT JOIN airlines a ON a.id = f.airline_id
GROUP BY a.name
ORDER BY flights DESC;

-- 7. One aircraft, several flight numbers: codeshares grouped under the operating flight.
SELECT f.flight_iata AS operating_flight, string_agg(c.flight_iata, ', ' ORDER BY c.flight_iata) AS also_sold_as
FROM flights f
JOIN flight_codeshares c ON c.flight_id = f.id
GROUP BY f.id, f.flight_iata
ORDER BY count(*) DESC;

-- 8. Local time vs UTC: the API's "+00:00" times are really local times.
--    Madrid -> Havana takes about 11 hours, not 5.
SELECT flight_iata, departure_iata, arrival_iata,
       departure_scheduled_local, arrival_scheduled_local,
       arrival_scheduled_utc - departure_scheduled_utc AS real_duration,
       arrival_scheduled_local - departure_scheduled_local AS naive_duration
FROM flights
WHERE departure_iata = 'MAD';

-- 9. Data-quality warnings kept with each flight.
SELECT flight_iata, departure_iata, unnest(warnings) AS warning
FROM flights
WHERE cardinality(warnings) > 0;

-- 10. Status breakdown (many records come without a status).
SELECT status, count(*) FROM flights GROUP BY status ORDER BY count(*) DESC;
