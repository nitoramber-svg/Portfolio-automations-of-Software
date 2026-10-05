CREATE OR REPLACE TABLE stg.sellers AS
SELECT DISTINCT ON (s.seller_id)
    trim(s.seller_id)                                AS seller_id,
    f.zip                                            AS zip_prefix,
    f.city,
    f.state
FROM raw.sellers s
JOIN stg.city_fixes f
  ON f.raw_city IS NOT DISTINCT FROM s.seller_city
 AND f.zip IS NOT DISTINCT FROM lpad(trim(s.seller_zip_code_prefix), 5, '0')
 AND f.state IS NOT DISTINCT FROM upper(trim(s.seller_state))
ORDER BY s.seller_id;
