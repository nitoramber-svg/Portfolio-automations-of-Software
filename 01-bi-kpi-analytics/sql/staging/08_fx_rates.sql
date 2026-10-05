CREATE OR REPLACE TABLE stg.fx_rates AS
SELECT
    CAST(rate_date AS DATE)    AS rate_date,
    upper(currency)            AS currency,
    CAST(rate AS DOUBLE)       AS rate,
    fx_source
FROM raw.fx_rates;
