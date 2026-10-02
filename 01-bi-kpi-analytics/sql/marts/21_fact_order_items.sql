-- One row per order line, with prices converted at the purchase-day exchange rate.
CREATE OR REPLACE TABLE mart.fact_order_items AS
SELECT
    i.order_id,
    i.item_seq,
    o.purchase_date_key,
    pr.product_key,
    s.seller_key,
    o.customer_key,
    o.customer_region_key,
    o.is_canceled,
    i.price_brl,
    i.freight_brl,
    round(i.price_brl * fx.brl_mxn, 2)   AS price_mxn,
    round(i.price_brl * fx.brl_usd, 2)   AS price_usd,
    round(i.freight_brl * fx.brl_mxn, 2) AS freight_mxn,
    round(i.freight_brl * fx.brl_usd, 2) AS freight_usd,
    fx.fx_source
FROM stg.order_items i
JOIN mart.fact_orders o          ON o.order_id = i.order_id
LEFT JOIN mart.dim_product pr    ON pr.product_id = i.product_id
LEFT JOIN mart.dim_seller s      ON s.seller_id = i.seller_id
LEFT JOIN mart.fx_daily fx       ON fx.date = CAST(o.purchased_at AS DATE);
