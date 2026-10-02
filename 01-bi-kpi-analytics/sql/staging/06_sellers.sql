CREATE OR REPLACE TABLE stg.sellers AS
SELECT DISTINCT ON (seller_id)
    trim(seller_id)                                  AS seller_id,
    lpad(trim(seller_zip_code_prefix), 5, '0')       AS zip_prefix,
    lower(strip_accents(trim(seller_city)))          AS city,
    upper(trim(seller_state))                        AS state
FROM raw.sellers
ORDER BY seller_id;
