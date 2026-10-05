CREATE OR REPLACE TABLE mart.dim_product AS
SELECT
    row_number() OVER (ORDER BY product_id) AS product_key,
    product_id,
    category_pt,
    category_en,
    category_es,
    category,
    weight_g,
    photos
FROM stg.products;
