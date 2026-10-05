CREATE OR REPLACE TABLE stg.order_items AS
SELECT
    trim(order_id)                                   AS order_id,
    TRY_CAST(order_item_id AS INTEGER)               AS item_seq,
    trim(product_id)                                 AS product_id,
    trim(seller_id)                                  AS seller_id,
    TRY_CAST(shipping_limit_date AS TIMESTAMP)       AS shipping_limit_at,
    TRY_CAST(price AS DECIMAL(12, 2))                AS price_brl,
    TRY_CAST(freight_value AS DECIMAL(12, 2))        AS freight_brl
FROM raw.order_items;
