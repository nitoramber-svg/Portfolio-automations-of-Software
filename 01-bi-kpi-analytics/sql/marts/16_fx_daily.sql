-- One BRL rate per calendar day: the latest published rate on or before that day (rates are
-- not published on weekends/holidays). Days before the first rate take the first rate.
CREATE OR REPLACE TABLE mart.fx_daily AS
WITH grid AS (
    SELECT d.date, c.currency
    FROM mart.dim_date d
    CROSS JOIN (SELECT DISTINCT currency FROM stg.fx_rates) c
),
first_rate AS (
    SELECT currency, arg_min(rate, rate_date) AS rate, arg_min(fx_source, rate_date) AS fx_source
    FROM stg.fx_rates
    GROUP BY currency
),
filled AS (
    SELECT g.date, g.currency,
           coalesce(r.rate, f.rate)           AS rate,
           coalesce(r.fx_source, f.fx_source) AS fx_source
    FROM grid g
    ASOF LEFT JOIN stg.fx_rates r ON r.currency = g.currency AND g.date >= r.rate_date
    JOIN first_rate f ON f.currency = g.currency
)
SELECT
    date,
    max(rate) FILTER (WHERE currency = 'MXN') AS brl_mxn,
    max(rate) FILTER (WHERE currency = 'USD') AS brl_usd,
    max(fx_source)                             AS fx_source
FROM filled
GROUP BY date;
