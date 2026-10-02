CREATE OR REPLACE TABLE mart.dim_seller AS
SELECT
    row_number() OVER (ORDER BY s.seller_id) AS seller_key,
    s.seller_id,
    s.city,
    s.state,
    r.region_key,
    r.region
FROM stg.sellers s
LEFT JOIN mart.dim_region r ON r.state = s.state;
