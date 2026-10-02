-- City names are free text, mostly typed by sellers: "sao paulo - sp", "auriflama/sp",
-- "belo horizont", "vendas@creditparts.com.br", "04482255", or just the state ("sp", "parana").
-- This builds one corrected city per (raw city, zip prefix, state); 05/06 join to it.
-- Rules, in order (measured on the real data; customers' cities come out unchanged):
--   cut   : drop what follows "/", ",", " - " or "(" and a trailing state code ("brasilia df")
--   junk  : only digits, an "@", < 3 letters, a state code, or the name of its own state when
--           that is not also one of its cities ("parana" in PR; "sao paulo" in SP stays)
--           -> the most common city of the zip prefix, or NULL
--   fuzzy : not a known city of its state, but >= 0.9 Jaro-Winkler to the zip prefix's city
--           ("garulhos" -> "guarulhos"); real districts that are merely absent stay as typed
CREATE OR REPLACE MACRO stg.norm_city(x) AS
    regexp_replace(lower(strip_accents(trim(x))), '\s+', ' ', 'g');

CREATE OR REPLACE TABLE stg.city_fixes AS
WITH geo AS (
    SELECT lpad(trim(geolocation_zip_code_prefix), 5, '0') AS zip,
           upper(trim(geolocation_state))                  AS state,
           stg.norm_city(geolocation_city)                 AS city
    FROM raw.geolocation
),
zip_city AS (
    SELECT zip, state, mode(city) AS city FROM geo GROUP BY ALL
),
known AS (
    SELECT DISTINCT state, city FROM geo
),
src AS (
    SELECT DISTINCT
        customer_city                                   AS raw_city,
        lpad(trim(customer_zip_code_prefix), 5, '0')    AS zip,
        upper(trim(customer_state))                     AS state
    FROM raw.customers
    UNION
    SELECT DISTINCT seller_city, lpad(trim(seller_zip_code_prefix), 5, '0'), upper(trim(seller_state))
    FROM raw.sellers
),
cut AS (
    SELECT *, stg.norm_city(raw_city) AS normalized,
           trim(regexp_replace(
               trim(regexp_replace(stg.norm_city(raw_city), '\s*(/|,|\s-\s|\().*$', '')),
               '[\s-]' || lower(state) || '$', '')) AS city_cut
    FROM src
),
judged AS (
    SELECT c.*, z.city AS zip_city, k.city IS NOT NULL AS is_known,
           regexp_matches(c.city_cut, '^[0-9 .-]+$|@')
           OR length(c.city_cut) < 3
           OR c.city_cut IN (SELECT lower(state) FROM seed.br_states)
           -- Without a city to put instead (no geolocation for the zip), a state name stays.
           OR (c.city_cut IN (SELECT lower(strip_accents(state_name)) FROM seed.br_states
                              WHERE state = c.state)
               AND k.city IS NULL AND z.city IS NOT NULL)  AS is_junk
    FROM cut c
    LEFT JOIN zip_city z ON z.zip = c.zip AND z.state = c.state
    LEFT JOIN known k    ON k.state = c.state AND k.city = c.city_cut
)
SELECT
    raw_city,
    zip,
    state,
    CASE
        WHEN is_junk THEN zip_city
        WHEN NOT is_known AND jaro_winkler_similarity(city_cut, zip_city) >= 0.9 THEN zip_city
        ELSE city_cut
    END AS city,
    CASE
        WHEN is_junk THEN 'junk'
        WHEN NOT is_known AND jaro_winkler_similarity(city_cut, zip_city) >= 0.9
             AND city_cut <> zip_city THEN 'fuzzy'
        WHEN city_cut <> normalized THEN 'cut'
    END AS fix_kind
FROM judged;
