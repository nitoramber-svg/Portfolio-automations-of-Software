CREATE OR REPLACE TABLE stg.order_payments AS
SELECT
    trim(order_id)                                   AS order_id,
    TRY_CAST(payment_sequential AS INTEGER)          AS payment_seq,
    lower(trim(payment_type))                        AS payment_type,
    TRY_CAST(payment_installments AS INTEGER)        AS installments,
    TRY_CAST(payment_value AS DECIMAL(12, 2))        AS payment_value_brl
FROM raw.order_payments;
