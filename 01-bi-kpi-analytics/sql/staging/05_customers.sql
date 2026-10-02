-- City names arrive with mixed case and accents ("São Paulo", "sao paulo"): 00_city_fixes
-- normalizes them and repairs the free-text mistakes.
CREATE OR REPLACE TABLE stg.customers AS
SELECT DISTINCT ON (c.customer_id)
    trim(c.customer_id)                              AS customer_id,
    trim(c.customer_unique_id)                       AS customer_unique_id,
    f.zip                                            AS zip_prefix,
    f.city,
    f.state
FROM raw.customers c
JOIN stg.city_fixes f
  ON f.raw_city IS NOT DISTINCT FROM c.customer_city
 AND f.zip IS NOT DISTINCT FROM lpad(trim(c.customer_zip_code_prefix), 5, '0')
 AND f.state IS NOT DISTINCT FROM upper(trim(c.customer_state))
ORDER BY c.customer_id;
