"""Data quality: checks on staging, quarantine, report and reconciliation.

Runs between the staging and mart layers. Every check returns the rows it flags; what happens
to them depends on the severity:

    quarantine  the row contradicts itself; it is moved out of staging (into quarantine.<table>,
                with the reason) before the marts are built, so it cannot reach a KPI
    fixed       staging already corrected it (duplicates, city names, missing translations)
    warning     kept; the KPIs that depend on it must know, and the report says how much
    info        looks wrong but is how the business works; reported so nobody "fixes" it

The checks are the problems measured on the real Olist data (see docs/data-quality.md), plus
structural ones that are zero there but would corrupt the model if a source changed.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

SEVERITIES = ("quarantine", "fixed", "warning", "info")

# How a row of each staging table is identified in quality.issues.
KEYS = {
    "orders": "order_id",
    "order_items": "order_id || '#' || item_seq",
    "order_payments": "order_id || '#' || payment_seq",
    "order_reviews": "review_id || '#' || order_id",
    "products": "product_id",
    "customers": "customer_id",
    "sellers": "seller_id",
}

# A month with fewer orders than this share of a typical (median) month is not comparable.
INCOMPLETE_MONTH_SHARE = 0.10

_MONTHS = """
    WITH monthly AS (
        SELECT date_trunc('month', purchased_at) AS month, count(*) AS n
        FROM stg.orders WHERE purchased_at IS NOT NULL GROUP BY 1
    ),
    months AS (
        SELECT CAST(gs AS DATE) AS month, coalesce(m.n, 0) AS n
        FROM (SELECT min(month) AS m0, max(month) AS m1 FROM monthly),
             generate_series(m0, m1, INTERVAL 1 MONTH) t(gs)
        LEFT JOIN monthly m ON m.month = gs
    )
"""

_ORDER_PAYMENTS = """
    WITH v AS (
        SELECT order_id, sum(price_brl) + sum(freight_brl) AS value_brl
        FROM stg.order_items GROUP BY 1
    ),
    p AS (
        SELECT order_id, sum(payment_value_brl) AS paid_brl, max(installments) AS installments
        FROM stg.order_payments GROUP BY 1
    ),
    d AS (
        SELECT order_id, paid_brl - value_brl AS diff, installments FROM v JOIN p USING (order_id)
    )
"""


@dataclass(frozen=True)
class Check:
    id: str
    table: str
    severity: str
    description: str
    # SELECT returning (row_key, order_id) of the flagged rows; order_id may be NULL.
    sql: str
    # SELECT count(*) of the rows the check looked at (the denominator of the report).
    total_sql: str = ""

    def total(self) -> str:
        return self.total_sql or f"SELECT count(*) FROM stg.{self.table}"


CHECKS: tuple[Check, ...] = (
    # --- orders -------------------------------------------------------------------------------
    Check(
        "orders_duplicated",
        "orders",
        "fixed",
        "Pedido repetido en la fuente; se conserva una sola copia",
        "SELECT trim(order_id), trim(order_id) FROM raw.orders GROUP BY 1 HAVING count(*) > 1",
        "SELECT count(DISTINCT trim(order_id)) FROM raw.orders",
    ),
    Check(
        "orders_unreadable_purchase",
        "orders",
        "quarantine",
        "Fecha de compra vacía o ilegible",
        "SELECT order_id, order_id FROM stg.orders WHERE purchased_at IS NULL",
    ),
    Check(
        "orders_before_purchase",
        "orders",
        "quarantine",
        "Aprobado o entregado antes de la compra",
        "SELECT order_id, order_id FROM stg.orders "
        "WHERE approved_at < purchased_at OR delivered_at < purchased_at",
    ),
    Check(
        "orders_delivered_without_date",
        "orders",
        "quarantine",
        "Estatus «entregado» sin fecha de entrega al cliente",
        "SELECT order_id, order_id FROM stg.orders "
        "WHERE order_status = 'delivered' AND delivered_at IS NULL",
    ),
    Check(
        "orders_canceled_but_delivered",
        "orders",
        "quarantine",
        "Cancelado o no disponible, pero con fecha de entrega: no se sabe si fue venta",
        "SELECT order_id, order_id FROM stg.orders "
        "WHERE order_status IN ('canceled', 'unavailable') AND delivered_at IS NOT NULL",
    ),
    Check(
        "orders_shipped_before_purchase",
        "orders",
        "warning",
        "Entregado a la paquetería antes de la compra (reloj del transportista, abr–ago 2018); "
        "no usar para tiempo de preparación",
        "SELECT order_id, order_id FROM stg.orders WHERE shipped_at < purchased_at",
    ),
    Check(
        "orders_shipped_before_approval",
        "orders",
        "warning",
        "Entregado a la paquetería antes de aprobar el pago; no usar para tiempo de preparación",
        "SELECT order_id, order_id FROM stg.orders WHERE shipped_at < approved_at",
    ),
    Check(
        "orders_delivered_before_shipped",
        "orders",
        "warning",
        "Entregado al cliente antes de salir con la paquetería; el tiempo total sí es válido",
        "SELECT order_id, order_id FROM stg.orders WHERE delivered_at < shipped_at",
    ),
    Check(
        "orders_active_without_items",
        "orders",
        "warning",
        "Pedido no cancelado sin productos: aporta 0 a las ventas",
        "SELECT o.order_id, o.order_id FROM stg.orders o "
        "WHERE o.order_status NOT IN ('canceled', 'unavailable') "
        "AND NOT EXISTS (SELECT 1 FROM stg.order_items i WHERE i.order_id = o.order_id)",
    ),
    Check(
        "orders_without_payment",
        "orders",
        "warning",
        "Pedido sin ningún registro de pago",
        "SELECT o.order_id, o.order_id FROM stg.orders o "
        "WHERE NOT EXISTS (SELECT 1 FROM stg.order_payments p WHERE p.order_id = o.order_id)",
    ),
    Check(
        "orders_installment_interest",
        "orders",
        "info",
        "Pagó más que precio + flete con tarjeta a meses: son intereses, no un error",
        _ORDER_PAYMENTS + "SELECT order_id, order_id FROM d WHERE diff > 0.01 AND installments > 1",
    ),
    Check(
        "orders_payment_mismatch",
        "orders",
        "warning",
        "Lo pagado no cuadra con precio + flete y no se explica por intereses",
        _ORDER_PAYMENTS + "SELECT order_id, order_id FROM d "
        "WHERE abs(diff) > 0.01 AND NOT (diff > 0 AND installments > 1)",
    ),
    Check(
        "orders_incomplete_month",
        "calendar",
        "warning",
        "Mes con menos del 10 % de los pedidos de un mes típico (arranque y corte del dataset): "
        "no compararlo",
        _MONTHS + "SELECT strftime(month, '%Y-%m'), NULL FROM months "
        f"WHERE n < {INCOMPLETE_MONTH_SHARE} * (SELECT median(n) FROM months)",
        _MONTHS + "SELECT count(*) FROM months",
    ),
    # --- order items ---------------------------------------------------------------------------
    Check(
        "items_orphan",
        "order_items",
        "quarantine",
        "Producto vendido de un pedido que no existe",
        f"SELECT {KEYS['order_items']}, order_id FROM stg.order_items i "
        "WHERE NOT EXISTS (SELECT 1 FROM stg.orders o WHERE o.order_id = i.order_id)",
    ),
    Check(
        "items_invalid_amount",
        "order_items",
        "quarantine",
        "Precio vacío o ≤ 0, o flete vacío o negativo",
        f"SELECT {KEYS['order_items']}, order_id FROM stg.order_items "
        "WHERE item_seq IS NULL OR price_brl IS NULL OR price_brl <= 0 "
        "OR freight_brl IS NULL OR freight_brl < 0",
    ),
    Check(
        "items_unknown_reference",
        "order_items",
        "warning",
        "Producto o vendedor que no está en su catálogo",
        f"SELECT {KEYS['order_items']}, order_id FROM stg.order_items i "
        "WHERE NOT EXISTS (SELECT 1 FROM stg.products p WHERE p.product_id = i.product_id) "
        "OR NOT EXISTS (SELECT 1 FROM stg.sellers s WHERE s.seller_id = i.seller_id)",
    ),
    # --- payments -------------------------------------------------------------------------------
    Check(
        "payments_invalid_amount",
        "order_payments",
        "quarantine",
        "Monto de pago vacío o negativo",
        f"SELECT {KEYS['order_payments']}, order_id FROM stg.order_payments "
        "WHERE payment_value_brl IS NULL OR payment_value_brl < 0",
    ),
    Check(
        "payments_type_not_defined",
        "order_payments",
        "warning",
        "Forma de pago «not_defined» o vacía",
        f"SELECT {KEYS['order_payments']}, order_id FROM stg.order_payments "
        "WHERE payment_type IS NULL OR payment_type = 'not_defined'",
    ),
    # --- reviews --------------------------------------------------------------------------------
    Check(
        "reviews_invalid_score",
        "order_reviews",
        "quarantine",
        "Calificación vacía o fuera de 1–5",
        f"SELECT {KEYS['order_reviews']}, order_id FROM stg.order_reviews "
        "WHERE score IS NULL OR score NOT BETWEEN 1 AND 5",
    ),
    Check(
        "reviews_orphan",
        "order_reviews",
        "quarantine",
        "Reseña de un pedido que no existe",
        f"SELECT {KEYS['order_reviews']}, order_id FROM stg.order_reviews r "
        "WHERE NOT EXISTS (SELECT 1 FROM stg.orders o WHERE o.order_id = r.order_id)",
    ),
    Check(
        "reviews_several_per_order",
        "order_reviews",
        "warning",
        "Pedido con varias reseñas (el cliente la volvió a contestar): los KPIs usan la última",
        f"SELECT {KEYS['order_reviews']}, order_id FROM stg.order_reviews "
        "QUALIFY count(*) OVER (PARTITION BY order_id) > 1",
    ),
    Check(
        "reviews_shared_by_orders",
        "order_reviews",
        "info",
        "Una misma reseña cubre varios pedidos del mismo cliente comprados juntos",
        f"SELECT {KEYS['order_reviews']}, order_id FROM stg.order_reviews "
        "QUALIFY count(*) OVER (PARTITION BY review_id) > 1",
    ),
    # --- catalog and places ---------------------------------------------------------------------
    Check(
        "products_without_category",
        "products",
        "warning",
        "Producto sin categoría: aparece como «Sin categoría»",
        "SELECT product_id, NULL FROM stg.products WHERE category_pt IS NULL",
    ),
    Check(
        "products_without_weight",
        "products",
        "warning",
        "Producto sin peso o con peso 0",
        "SELECT product_id, NULL FROM stg.products WHERE weight_g IS NULL OR weight_g = 0",
    ),
    Check(
        "products_untranslated_category",
        "products",
        "fixed",
        "Categoría sin traducción oficial de Olist; la cubre la tabla en español",
        "SELECT product_id, NULL FROM stg.products "
        "WHERE category_pt IS NOT NULL AND category_en IS NULL AND category_es IS NOT NULL",
    ),
    Check(
        "sellers_city_fixed",
        "sellers",
        "fixed",
        "Ciudad del vendedor mal escrita (estado pegado, typo, correo, CP); se corrigió",
        "SELECT DISTINCT trim(s.seller_id), NULL FROM raw.sellers s JOIN stg.city_fixes f "
        "ON f.raw_city IS NOT DISTINCT FROM s.seller_city "
        "AND f.zip IS NOT DISTINCT FROM lpad(trim(s.seller_zip_code_prefix), 5, '0') "
        "AND f.state IS NOT DISTINCT FROM upper(trim(s.seller_state)) "
        "WHERE f.fix_kind IS NOT NULL",
    ),
    Check(
        "customers_city_fixed",
        "customers",
        "fixed",
        "Ciudad del cliente mal escrita; se corrigió",
        "SELECT DISTINCT trim(c.customer_id), NULL FROM raw.customers c JOIN stg.city_fixes f "
        "ON f.raw_city IS NOT DISTINCT FROM c.customer_city "
        "AND f.zip IS NOT DISTINCT FROM lpad(trim(c.customer_zip_code_prefix), 5, '0') "
        "AND f.state IS NOT DISTINCT FROM upper(trim(c.customer_state)) "
        "WHERE f.fix_kind IS NOT NULL",
    ),
    Check(
        "sellers_without_city",
        "sellers",
        "warning",
        "Vendedor sin ciudad: la escrita no era ciudad y su CP no está en la geolocalización",
        "SELECT seller_id, NULL FROM stg.sellers WHERE city IS NULL",
    ),
    Check(
        "sellers_state_differs_from_zip",
        "sellers",
        "warning",
        "El estado declarado no es el de su CP (que pertenece a un solo estado); "
        "los análisis por región del vendedor usan el declarado",
        "WITH g AS (SELECT lpad(trim(geolocation_zip_code_prefix), 5, '0') AS zip, "
        "min(upper(trim(geolocation_state))) AS state FROM raw.geolocation GROUP BY 1 "
        "HAVING count(DISTINCT upper(trim(geolocation_state))) = 1) "
        "SELECT s.seller_id, NULL FROM stg.sellers s JOIN g ON g.zip = s.zip_prefix "
        "WHERE g.state <> s.state",
    ),
    Check(
        "customers_unknown_state",
        "customers",
        "warning",
        "Cliente con un estado que no existe en Brasil",
        "SELECT customer_id, NULL FROM stg.customers "
        "WHERE state IS NULL OR state NOT IN (SELECT state FROM seed.br_states)",
    ),
)

# Children of a quarantined order follow it, so no table keeps half an order.
CASCADE = ("order_items", "order_payments", "order_reviews")
CASCADE_REASON = "parent_order_quarantined"


class ReconciliationError(RuntimeError):
    """The marts lost or duplicated data: refuse to publish them."""


def run_checks(con: duckdb.DuckDBPyConnection, checks: tuple[Check, ...] = CHECKS) -> None:
    """Flag rows into quality.issues and record each check (with its denominator)."""
    con.execute("CREATE SCHEMA IF NOT EXISTS quality")
    con.execute("CREATE SCHEMA IF NOT EXISTS quarantine")
    # Snapshot before anything leaves staging: reconciliation and the report's BRL impact.
    con.execute(
        "CREATE OR REPLACE TABLE quality.staged AS SELECT "
        "(SELECT count(*) FROM stg.orders) AS orders, "
        "(SELECT count(*) FROM stg.order_items) AS items, "
        "(SELECT coalesce(sum(price_brl), 0) FROM stg.order_items) AS price_brl"
    )
    con.execute(
        "CREATE OR REPLACE TABLE quality.order_value AS "
        "SELECT order_id, sum(coalesce(price_brl, 0) + coalesce(freight_brl, 0)) AS value_brl "
        "FROM stg.order_items GROUP BY 1"
    )
    con.execute(
        "CREATE OR REPLACE TABLE quality.checks (check_id VARCHAR, table_name VARCHAR, "
        "severity VARCHAR, description VARCHAR, total BIGINT)"
    )
    con.execute(
        "CREATE OR REPLACE TABLE quality.issues (check_id VARCHAR, table_name VARCHAR, "
        "severity VARCHAR, row_key VARCHAR, order_id VARCHAR)"
    )
    for c in checks:
        total = con.execute(c.total()).fetchone()[0]
        con.execute(
            "INSERT INTO quality.checks VALUES (?, ?, ?, ?, ?)",
            [c.id, c.table, c.severity, c.description, total],
        )
        con.execute(
            f"INSERT INTO quality.issues SELECT DISTINCT ?, ?, ?, k, o FROM ({c.sql}) t(k, o)",
            [c.id, c.table, c.severity],
        )


def quarantine(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Move quarantined rows (and the children of quarantined orders) out of staging."""
    moved: dict[str, int] = {}
    for table, key in KEYS.items():
        reasons = (
            "SELECT row_key, check_id FROM quality.issues "
            f"WHERE severity = 'quarantine' AND table_name = '{table}'"
        )
        if table in CASCADE:
            reasons += (
                f" UNION ALL SELECT {key}, '{CASCADE_REASON}' FROM stg.{table} WHERE order_id IN "
                "(SELECT row_key FROM quality.issues "
                "WHERE severity = 'quarantine' AND table_name = 'orders')"
            )
        con.execute(
            f"CREATE OR REPLACE TABLE quarantine.{table} AS "
            f"WITH r AS (SELECT row_key, string_agg(DISTINCT check_id, ', ' ORDER BY check_id) "
            f"AS reasons FROM ({reasons}) GROUP BY row_key) "
            f"SELECT s.*, r.reasons FROM stg.{table} s JOIN r ON r.row_key = ({key})"
        )
        con.execute(f"DELETE FROM stg.{table} WHERE ({key}) IN (SELECT row_key FROM ({reasons}))")
        moved[table] = con.execute(f"SELECT count(*) FROM quarantine.{table}").fetchone()[0]
    return moved


def build_report(con: duckdb.DuckDBPyConnection) -> None:
    """quality.report: one row per check, including those that found nothing."""
    con.execute(
        """
        CREATE OR REPLACE TABLE quality.report AS
        WITH hits AS (
            SELECT check_id, count(DISTINCT row_key) AS rows,
                   count(DISTINCT order_id)          AS orders
            FROM quality.issues GROUP BY 1
        ),
        value AS (
            SELECT i.check_id, sum(v.value_brl) AS value_brl
            FROM (SELECT DISTINCT check_id, order_id FROM quality.issues
                  WHERE order_id IS NOT NULL) i
            JOIN quality.order_value v USING (order_id)
            GROUP BY 1
        )
        SELECT
            c.check_id, c.table_name, c.severity, c.description,
            coalesce(h.rows, 0)                                    AS rows,
            c.total,
            round(100.0 * coalesce(h.rows, 0) / nullif(c.total, 0), 3) AS pct,
            coalesce(h.orders, 0)                                  AS orders,
            coalesce(v.value_brl, 0)                               AS value_brl
        FROM quality.checks c
        LEFT JOIN hits h  USING (check_id)
        LEFT JOIN value v USING (check_id)
        ORDER BY list_position(['quarantine', 'fixed', 'warning', 'info'], c.severity),
                 rows DESC, c.check_id
        """
    )


def run(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Checks, quarantine and report; returns the rows quarantined per table."""
    run_checks(con)
    moved = quarantine(con)
    build_report(con)
    return moved


def reconcile(con: duckdb.DuckDBPyConnection) -> list[tuple[str, float, float]]:
    """After the marts: staged = modeled + quarantined, and fact keys are unique.

    Raises ReconciliationError instead of letting a dashboard show numbers that don't add up.
    """
    checks = {
        "orders: staged = fact_orders + quarantine": (
            "SELECT orders FROM quality.staged",
            "SELECT (SELECT count(*) FROM mart.fact_orders)"
            " + (SELECT count(*) FROM quarantine.orders)",
        ),
        "items: staged = fact_order_items + quarantine": (
            "SELECT items FROM quality.staged",
            "SELECT (SELECT count(*) FROM mart.fact_order_items)"
            " + (SELECT count(*) FROM quarantine.order_items)",
        ),
        "price BRL: staged = fact_order_items + quarantine": (
            "SELECT price_brl FROM quality.staged",
            "SELECT (SELECT coalesce(sum(price_brl), 0) FROM mart.fact_order_items)"
            " + (SELECT coalesce(sum(price_brl), 0) FROM quarantine.order_items)",
        ),
        "fact_orders: one row per order": (
            "SELECT count(DISTINCT order_id) FROM mart.fact_orders",
            "SELECT count(*) FROM mart.fact_orders",
        ),
        "fact_order_items: one row per line": (
            "SELECT count(DISTINCT (order_id, item_seq)) FROM mart.fact_order_items",
            "SELECT count(*) FROM mart.fact_order_items",
        ),
    }
    results = []
    for name, (expected_sql, actual_sql) in checks.items():
        expected = float(con.execute(expected_sql).fetchone()[0])
        actual = float(con.execute(actual_sql).fetchone()[0])
        results.append((name, expected, actual))
    con.execute(
        "CREATE OR REPLACE TABLE quality.reconciliation "
        "(name VARCHAR, expected DOUBLE, actual DOUBLE, ok BOOLEAN)"
    )
    con.executemany(
        "INSERT INTO quality.reconciliation VALUES (?, ?, ?, ?)",
        [(n, e, a, abs(e - a) < 0.005) for n, e, a in results],
    )
    failed = [f"{n}: expected {e:,.2f}, got {a:,.2f}" for n, e, a in results if abs(e - a) >= 0.005]
    if failed:
        raise ReconciliationError("; ".join(failed))
    return results
