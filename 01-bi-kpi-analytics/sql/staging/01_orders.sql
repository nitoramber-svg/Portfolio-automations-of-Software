-- One row per order, typed. Duplicate order_ids keep the first occurrence.
CREATE OR REPLACE TABLE stg.orders AS
SELECT DISTINCT ON (order_id)
    trim(order_id)                                                  AS order_id,
    trim(customer_id)                                               AS customer_id,
    lower(trim(order_status))                                       AS order_status,
    TRY_CAST(order_purchase_timestamp AS TIMESTAMP)                 AS purchased_at,
    TRY_CAST(order_approved_at AS TIMESTAMP)                        AS approved_at,
    TRY_CAST(order_delivered_carrier_date AS TIMESTAMP)             AS shipped_at,
    TRY_CAST(order_delivered_customer_date AS TIMESTAMP)            AS delivered_at,
    CAST(TRY_CAST(order_estimated_delivery_date AS TIMESTAMP) AS DATE) AS estimated_delivery
FROM raw.orders
WHERE nullif(trim(order_id), '') IS NOT NULL
ORDER BY order_id;
