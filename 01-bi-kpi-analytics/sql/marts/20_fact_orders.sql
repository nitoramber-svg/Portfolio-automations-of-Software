-- One row per order: value, payment and delivery performance.
CREATE OR REPLACE TABLE mart.fact_orders AS
WITH items AS (
    SELECT order_id, count(*) AS items, sum(price_brl) AS items_brl, sum(freight_brl) AS freight_brl
    FROM stg.order_items
    GROUP BY order_id
),
payments AS (
    SELECT order_id,
           sum(payment_value_brl)                    AS payment_value_brl,
           max(installments)                         AS installments,
           arg_max(payment_type, payment_value_brl)  AS main_payment_type
    FROM stg.order_payments
    GROUP BY order_id
)
SELECT
    o.order_id,
    CAST(strftime(o.purchased_at, '%Y%m%d') AS INTEGER)  AS purchase_date_key,
    c.customer_key,
    c.region_key                                          AS customer_region_key,
    pt.payment_type_key,
    o.order_status                                        AS status,
    o.order_status IN ('canceled', 'unavailable')         AS is_canceled,
    o.purchased_at,
    o.approved_at,
    o.shipped_at,
    o.delivered_at,
    o.estimated_delivery,
    coalesce(i.items, 0)                                  AS items,
    coalesce(i.items_brl, 0)                              AS items_brl,
    coalesce(i.freight_brl, 0)                            AS freight_brl,
    coalesce(i.items_brl, 0) + coalesce(i.freight_brl, 0) AS order_value_brl,
    p.payment_value_brl,
    p.installments,
    date_diff('day', o.purchased_at, o.delivered_at)      AS delivery_days,
    CASE WHEN o.delivered_at IS NOT NULL
         THEN greatest(date_diff('day', o.estimated_delivery, CAST(o.delivered_at AS DATE)), 0)
    END                                                   AS days_late,
    CASE WHEN o.delivered_at IS NOT NULL
         THEN CAST(o.delivered_at AS DATE) <= o.estimated_delivery
    END                                                   AS on_time
FROM stg.orders o
LEFT JOIN mart.dim_customer c       ON c.customer_id = o.customer_id
LEFT JOIN items i                   ON i.order_id = o.order_id
LEFT JOIN payments p                ON p.order_id = o.order_id
LEFT JOIN mart.dim_payment_type pt  ON pt.payment_type = p.main_payment_type;
