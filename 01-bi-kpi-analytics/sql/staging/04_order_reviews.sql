CREATE OR REPLACE TABLE stg.order_reviews AS
SELECT
    trim(review_id)                                  AS review_id,
    trim(order_id)                                   AS order_id,
    TRY_CAST(review_score AS INTEGER)                AS score,
    coalesce(nullif(trim(review_comment_message), ''),
             nullif(trim(review_comment_title), '')) IS NOT NULL AS has_comment,
    TRY_CAST(review_creation_date AS TIMESTAMP)      AS created_at,
    TRY_CAST(review_answer_timestamp AS TIMESTAMP)   AS answered_at
FROM raw.order_reviews;
