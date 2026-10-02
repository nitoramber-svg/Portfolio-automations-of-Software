-- Calendar covering every purchase, delivery and estimated delivery date.
CREATE OR REPLACE TABLE mart.dim_date AS
WITH bounds AS (
    SELECT
        min(CAST(purchased_at AS DATE)) AS d0,
        greatest(max(CAST(delivered_at AS DATE)), max(estimated_delivery),
                 max(CAST(purchased_at AS DATE))) AS d1
    FROM stg.orders
),
days AS (
    SELECT CAST(gs AS DATE) AS date
    FROM bounds, generate_series(CAST(d0 AS TIMESTAMP), CAST(d1 AS TIMESTAMP), INTERVAL 1 DAY) t(gs)
)
SELECT
    CAST(strftime(d.date, '%Y%m%d') AS INTEGER)            AS date_key,
    d.date,
    year(d.date)                                            AS year,
    month(d.date)                                           AS month,
    CAST(strftime(d.date, '%Y%m') AS INTEGER)               AS year_month,
    weekofyear(d.date)                                      AS week,
    isodow(d.date)                                          AS iso_weekday,
    h.name                                                  AS holiday_name,
    coalesce(h.kind = 'holiday', false)                     AS is_holiday_br,
    coalesce(h.name = 'Black Friday', false)                AS is_black_friday
FROM days d
LEFT JOIN seed.br_holidays h ON h.date = d.date;
