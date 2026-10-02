CREATE OR REPLACE TABLE mart.dim_region AS
SELECT
    row_number() OVER (ORDER BY region, state) AS region_key,
    region,
    state,
    state_name
FROM seed.br_states;
