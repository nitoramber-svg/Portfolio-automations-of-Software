-- Grain: one row per (review, order). Olist links one review to every order bought together,
-- and some orders were reviewed more than once: KPIs per order use is_latest_for_order.
CREATE OR REPLACE TABLE mart.fact_reviews AS
SELECT
    r.review_id,
    r.order_id,
    CAST(strftime(r.created_at, '%Y%m%d') AS INTEGER) AS created_date_key,
    o.customer_region_key,
    r.score,
    r.score <= 2                                      AS is_negative,
    r.has_comment,
    date_diff('hour', r.created_at, r.answered_at)    AS answer_hours,
    row_number() OVER (PARTITION BY r.order_id
                       ORDER BY r.created_at DESC, r.answered_at DESC, r.review_id) = 1
                                                      AS is_latest_for_order
FROM stg.order_reviews r
LEFT JOIN mart.fact_orders o ON o.order_id = r.order_id;
