-- Pre-aggregated sales for the dashboard: day x customer region x category.
-- Canceled/unavailable orders are excluded from sales.
CREATE OR REPLACE TABLE mart.agg_sales_daily AS
SELECT
    f.purchase_date_key                  AS date_key,
    f.customer_region_key                AS region_key,
    p.category,
    count(DISTINCT f.order_id)           AS orders,
    count(*)                             AS items,
    sum(f.price_brl)                     AS gmv_brl,
    sum(f.price_mxn)                     AS gmv_mxn,
    sum(f.price_usd)                     AS gmv_usd,
    sum(f.freight_brl)                   AS freight_brl
FROM mart.fact_order_items f
LEFT JOIN mart.dim_product p ON p.product_key = f.product_key
WHERE NOT f.is_canceled
GROUP BY ALL;
