-- Category label: Spanish if we have it, else Olist's English translation, else the raw name.
CREATE OR REPLACE TABLE stg.products AS
SELECT DISTINCT ON (p.product_id)
    trim(p.product_id)                                           AS product_id,
    nullif(trim(p.product_category_name), '')                    AS category_pt,
    t.product_category_name_english                              AS category_en,
    s.category_es                                                AS category_es,
    coalesce(s.category_es, t.product_category_name_english,
             nullif(trim(p.product_category_name), ''), 'Sin categoría') AS category,
    TRY_CAST(p.product_weight_g AS INTEGER)                      AS weight_g,
    TRY_CAST(p.product_photos_qty AS INTEGER)                    AS photos
FROM raw.products p
LEFT JOIN raw.category_translation t
       ON t.product_category_name = trim(p.product_category_name)
LEFT JOIN seed.category_es s
       ON s.category_pt = trim(p.product_category_name)
ORDER BY p.product_id;
