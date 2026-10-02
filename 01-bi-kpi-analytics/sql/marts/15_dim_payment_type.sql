CREATE OR REPLACE TABLE mart.dim_payment_type AS
SELECT
    row_number() OVER (ORDER BY payment_type) AS payment_type_key,
    payment_type
FROM (SELECT DISTINCT payment_type FROM stg.order_payments WHERE payment_type IS NOT NULL);
