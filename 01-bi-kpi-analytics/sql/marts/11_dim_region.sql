-- lat/lng: median of the state's geolocation points, for the map. The median, because the
-- geolocation file has points far outside Brazil. NULL when that optional file is absent.
CREATE OR REPLACE TABLE mart.dim_region AS
WITH centers AS (
    SELECT upper(trim(geolocation_state))                  AS state,
           median(TRY_CAST(geolocation_lat AS DOUBLE))      AS lat,
           median(TRY_CAST(geolocation_lng AS DOUBLE))      AS lng
    FROM raw.geolocation
    GROUP BY 1
)
SELECT
    row_number() OVER (ORDER BY s.region, s.state) AS region_key,
    s.region,
    s.state,
    s.state_name,
    c.lat,
    c.lng
FROM seed.br_states s
LEFT JOIN centers c ON c.state = s.state;
