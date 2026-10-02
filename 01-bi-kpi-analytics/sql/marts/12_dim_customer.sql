-- Olist issues a new customer_id per order; customer_unique_id identifies the person.
CREATE OR REPLACE TABLE mart.dim_customer AS
SELECT
    row_number() OVER (ORDER BY c.customer_id) AS customer_key,
    c.customer_id,
    c.customer_unique_id,
    c.city,
    c.state,
    r.region_key,
    r.region
FROM stg.customers c
LEFT JOIN mart.dim_region r ON r.state = c.state;
