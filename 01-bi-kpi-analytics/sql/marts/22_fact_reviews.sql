CREATE OR REPLACE TABLE mart.fact_reviews AS
SELECT
    r.review_id,
    r.order_id,
    CAST(strftime(r.created_at, '%Y%m%d') AS INTEGER) AS created_date_key,
    o.customer_region_key,
    r.score,
    r.score <= 2                                      AS is_negative,
    r.has_comment,
    date_diff('hour', r.created_at, r.answered_at)    AS answer_hours
FROM stg.order_reviews r
LEFT JOIN mart.fact_orders o ON o.order_id = r.order_id;
