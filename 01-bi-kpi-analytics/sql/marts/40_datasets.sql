-- The three datasets the KPI engine and the security layer read: the star schema flattened to
-- the attributes a user can filter, group or be restricted by. Nothing reads the facts directly.
-- Materialized as tables, not views (the "v_" names predate that): every dashboard query would
-- otherwise redo these joins. Measured: the executive summary went from 1.9 s to 0.64 s, for
-- 1.5 s more per load (docs/benchmark.md).
-- Every dataset is dated by the order's purchase date, so one date filter means the same thing
-- for sales, deliveries and reviews.

-- One row per order line. The only grain where a seller owns the row (1,278 orders mix sellers).
CREATE OR REPLACE TABLE mart.v_sales AS
SELECT
    f.order_id,
    f.item_seq,
    d.date,
    d.year_month,
    d.is_complete_month,
    c.region               AS customer_region,
    c.state                AS customer_state,
    c.customer_unique_id,
    c.city                 AS customer_city,
    s.seller_id,
    s.region               AS seller_region,
    s.state                AS seller_state,
    p.category,
    pt.payment_type,
    f.is_canceled,
    f.price_brl,
    f.price_mxn,
    f.price_usd,
    f.freight_brl,
    f.freight_mxn,
    f.freight_usd
FROM mart.fact_order_items f
JOIN mart.dim_date d               ON d.date_key = f.purchase_date_key
JOIN mart.fact_orders o            ON o.order_id = f.order_id
LEFT JOIN mart.dim_customer c      ON c.customer_key = f.customer_key
LEFT JOIN mart.dim_seller s        ON s.seller_key = f.seller_key
LEFT JOIN mart.dim_product p       ON p.product_key = f.product_key
LEFT JOIN mart.dim_payment_type pt ON pt.payment_type_key = o.payment_type_key;

-- One row per order.
CREATE OR REPLACE TABLE mart.v_orders AS
SELECT
    o.order_id,
    d.date,
    d.year_month,
    d.is_complete_month,
    c.region               AS customer_region,
    c.state                AS customer_state,
    c.customer_unique_id,
    c.city                 AS customer_city,
    pt.payment_type,
    o.status,
    o.is_canceled,
    o.delivered_at IS NOT NULL AS is_delivered,
    o.on_time,
    o.delivery_days,
    o.days_late,
    o.order_value_brl,
    o.payment_value_brl,
    o.installments
FROM mart.fact_orders o
JOIN mart.dim_date d               ON d.date_key = o.purchase_date_key
LEFT JOIN mart.dim_customer c      ON c.customer_key = o.customer_key
LEFT JOIN mart.dim_payment_type pt ON pt.payment_type_key = o.payment_type_key;

-- One row per reviewed order: its latest review (see 22_fact_reviews.sql).
CREATE OR REPLACE TABLE mart.v_reviews AS
SELECT
    r.review_id,
    o.order_id,
    o.date,
    o.year_month,
    o.is_complete_month,
    o.customer_region,
    o.customer_state,
    o.customer_unique_id,
    o.customer_city,
    o.payment_type,
    r.score,
    r.is_negative,
    r.has_comment,
    o.days_late
FROM mart.fact_reviews r
JOIN mart.v_orders o ON o.order_id = r.order_id
WHERE r.is_latest_for_order;
