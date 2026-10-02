-- Calendar covering every purchase, delivery and estimated delivery date.
-- is_complete_month: inside the purchase window and not flagged as incomplete by the quality
-- checks (the dataset starts and ends mid-stream); trends and anomalies should skip the rest.
CREATE OR REPLACE TABLE mart.dim_date AS
WITH bounds AS (
    SELECT
        min(CAST(purchased_at AS DATE)) AS d0,
        greatest(max(CAST(delivered_at AS DATE)), max(estimated_delivery),
                 max(CAST(purchased_at AS DATE))) AS d1
    FROM stg.orders
),
incomplete AS (
    SELECT row_key AS ym FROM quality.issues WHERE check_id = 'orders_incomplete_month'
),
purchase_months AS (
    SELECT min(date_trunc('month', purchased_at)) AS m0, max(date_trunc('month', purchased_at)) AS m1
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
    coalesce(h.name = 'Black Friday', false)                AS is_black_friday,
    date_trunc('month', d.date) BETWEEN pm.m0 AND pm.m1
        AND strftime(d.date, '%Y-%m') NOT IN (SELECT ym FROM incomplete) AS is_complete_month
FROM days d
CROSS JOIN purchase_months pm
LEFT JOIN seed.br_holidays h ON h.date = d.date;
