-- City names arrive with mixed case and accents ("São Paulo", "sao paulo"): normalize them.
CREATE OR REPLACE TABLE stg.customers AS
SELECT DISTINCT ON (customer_id)
    trim(customer_id)                                AS customer_id,
    trim(customer_unique_id)                         AS customer_unique_id,
    lpad(trim(customer_zip_code_prefix), 5, '0')     AS zip_prefix,
    lower(strip_accents(trim(customer_city)))        AS city,
    upper(trim(customer_state))                      AS state
FROM raw.customers
ORDER BY customer_id;
