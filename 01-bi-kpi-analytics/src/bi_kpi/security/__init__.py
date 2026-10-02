"""Row-level security, enforced where the data is read, not in the interface.

Every read of a dataset goes through ``relation()``: it returns the SQL of the rows and columns
the user may see. The KPI engine aggregates over it; ``secure_rows()`` returns rows from it. A
user who is not in config/users.yaml gets nothing (fail closed).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

ROLES = ("director", "regional_manager", "analyst", "seller")
DATASETS = ("v_sales", "v_orders", "v_reviews")

# Columns that identify an individual customer: only director and regional managers see them.
CUSTOMER_COLUMNS = frozenset({"customer_unique_id", "customer_city"})

# Order-level money that includes every seller's lines; a seller must not see it, not even
# aggregated (1,278 orders mix sellers).
ORDER_TOTAL_COLUMNS = frozenset({"order_value_brl", "payment_value_brl"})


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class User:
    name: str
    role: str
    regions: tuple[str, ...] = ()
    seller_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"{self.name}: unknown role {self.role!r}")
        if self.role == "regional_manager" and not self.regions:
            raise ValueError(f"{self.name}: a regional manager needs regions")
        if self.role == "seller" and not self.seller_id:
            raise ValueError(f"{self.name}: a seller needs seller_id")

    @property
    def sees_customers(self) -> bool:
        return self.role in ("director", "regional_manager")


@dataclass(frozen=True)
class Users:
    by_name: dict[str, User] = field(default_factory=dict)

    def get(self, name: str) -> User:
        try:
            return self.by_name[name]
        except KeyError:
            raise AccessDenied(f"unknown user {name!r}") from None


def load_users(path: Path) -> Users:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["users"]
    return Users(
        {
            name: User(
                name=name,
                role=spec["role"],
                regions=tuple(spec.get("regions", ())),
                seller_id=spec.get("seller_id"),
            )
            for name, spec in raw.items()
        }
    )


def _columns(con: duckdb.DuckDBPyConnection, dataset: str) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'mart' AND table_name = ? ORDER BY ordinal_position",
            [dataset],
        ).fetchall()
    ]


def visible_columns(con: duckdb.DuckDBPyConnection, user: User, dataset: str) -> list[str]:
    hidden: set[str] = set()
    if user.role == "seller":
        hidden |= ORDER_TOTAL_COLUMNS
    if not user.sees_customers:
        hidden |= CUSTOMER_COLUMNS
    return [c for c in _columns(con, dataset) if c not in hidden]


def relation(
    con: duckdb.DuckDBPyConnection, user: User, dataset: str, *, aggregate: bool = False
) -> tuple[str, list]:
    """SQL (and its parameters) of the part of ``dataset`` that ``user`` may read.

    ``aggregate=True`` keeps the customer columns for counting (unique customers, repeat rate)
    — the engine only returns aggregates — but never the order totals a seller must not see.
    """
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}")
    cols = _columns(con, dataset)
    if user.role == "seller":
        cols = [c for c in cols if c not in ORDER_TOTAL_COLUMNS]
    if not aggregate and not user.sees_customers:
        cols = [c for c in cols if c not in CUSTOMER_COLUMNS]
    select = ", ".join(cols)

    if user.role in ("director", "analyst"):
        return f"SELECT {select} FROM mart.{dataset}", []
    if user.role == "regional_manager":
        marks = ", ".join("?" for _ in user.regions)
        return (
            f"SELECT {select} FROM mart.{dataset} WHERE customer_region IN ({marks})",
            list(user.regions),
        )
    # seller
    if dataset == "v_sales":
        return f"SELECT {select} FROM mart.v_sales WHERE seller_id = ?", [user.seller_id]
    return (
        f"SELECT {select} FROM mart.{dataset} WHERE order_id IN "
        "(SELECT order_id FROM mart.v_sales WHERE seller_id = ?)",
        [user.seller_id],
    )


def secure_rows(
    con: duckdb.DuckDBPyConnection,
    user: User,
    dataset: str,
    columns: list[str] | None = None,
    limit: int | None = None,
):
    """Rows of ``dataset`` for ``user`` as a DataFrame. Asking for a hidden column is an error."""
    sql, params = relation(con, user, dataset)
    allowed = visible_columns(con, user, dataset)
    if columns:
        denied = [c for c in columns if c not in allowed]
        if denied:
            raise AccessDenied(f"{user.name} may not read {denied} from {dataset}")
        sql = f"SELECT {', '.join(columns)} FROM ({sql})"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, params).df()


def export_rls_rules(users: Users, path: Path) -> Path:
    """Write the rules as a QuickSight / Quick Suite permissions dataset.

    One row per user; an empty cell means "all values". Quick Suite filters a dataset by
    matching columns, so it can apply customer_region to every dataset but seller_id only to
    one at line grain (v_sales); the order and review datasets need a seller_id column per row
    to be restricted there — see docs/quicksuite.md (step 6).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["UserName", "customer_region", "seller_id"])
        for user in users.by_name.values():
            if user.role == "regional_manager":
                for region in user.regions:
                    w.writerow([user.name, region, ""])
            elif user.role == "seller":
                w.writerow([user.name, "", user.seller_id])
            else:
                w.writerow([user.name, "", ""])
    return path
