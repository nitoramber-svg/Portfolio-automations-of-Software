"""Synthetic data in the exact Olist schema, so the project runs without a Kaggle account.

It is NOT Olist data. It mimics Olist's shape closely enough to exercise every part of the
pipeline: regional skew towards São Paulo, growth, weekly seasonality, a Black Friday spike,
a May-2018 truckers' strike that delays deliveries, a truncated tail, and a small share of
deliberately broken rows for the data-quality checks.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from bi_kpi.olist import FILES

# Share of customers per state, roughly following Olist's distribution.
CUSTOMER_STATES = {
    "SP": 42.0,
    "RJ": 12.9,
    "MG": 11.7,
    "RS": 5.5,
    "PR": 5.1,
    "SC": 3.7,
    "BA": 3.4,
    "DF": 2.2,
    "ES": 2.0,
    "GO": 2.0,
    "PE": 1.7,
    "CE": 1.3,
    "PA": 1.0,
    "MT": 0.9,
    "MA": 0.8,
    "MS": 0.7,
    "PB": 0.5,
    "PI": 0.5,
    "RN": 0.5,
    "AL": 0.4,
    "SE": 0.3,
    "TO": 0.3,
    "RO": 0.3,
    "AM": 0.2,
    "AC": 0.1,
    "AP": 0.1,
    "RR": 0.1,
}
SELLER_STATES = {
    "SP": 60.0,
    "PR": 11.0,
    "MG": 8.0,
    "SC": 6.0,
    "RJ": 5.0,
    "RS": 4.0,
    "GO": 2.0,
    "DF": 1.5,
    "ES": 1.0,
    "BA": 1.0,
    "PE": 0.5,
}
# Typical delivery days by customer state (far from São Paulo → slower).
SLOW_STATES = {
    "AM",
    "AP",
    "RR",
    "AC",
    "PA",
    "RO",
    "TO",
    "MA",
    "PI",
    "CE",
    "RN",
    "PB",
    "PE",
    "AL",
    "SE",
    "BA",
}
CITIES = {
    "SP": ["sao paulo", "campinas", "guarulhos", "santos"],
    "RJ": ["rio de janeiro", "niteroi"],
    "MG": ["belo horizonte", "uberlandia"],
    "RS": ["porto alegre"],
    "PR": ["curitiba", "londrina"],
}
STATE_CENTROIDS = {
    "AC": (-9.0, -70.5),
    "AL": (-9.6, -36.6),
    "AP": (1.4, -51.8),
    "AM": (-3.4, -65.0),
    "BA": (-12.5, -41.7),
    "CE": (-5.2, -39.5),
    "DF": (-15.8, -47.9),
    "ES": (-19.6, -40.7),
    "GO": (-15.9, -49.8),
    "MA": (-5.4, -45.4),
    "MT": (-12.6, -55.9),
    "MS": (-20.5, -54.8),
    "MG": (-18.5, -44.6),
    "PA": (-3.8, -52.5),
    "PB": (-7.1, -36.8),
    "PR": (-24.6, -51.6),
    "PE": (-8.4, -37.9),
    "PI": (-7.4, -42.7),
    "RJ": (-22.3, -42.7),
    "RN": (-5.8, -36.6),
    "RS": (-29.8, -53.2),
    "RO": (-10.9, -62.8),
    "RR": (2.0, -61.4),
    "SC": (-27.3, -50.2),
    "SP": (-22.2, -48.7),
    "SE": (-10.6, -37.4),
    "TO": (-10.2, -48.3),
}
PAYMENT_TYPES = ["credit_card", "boleto", "voucher", "debit_card"]
PAYMENT_WEIGHTS = [0.74, 0.19, 0.05, 0.02]

START = date(2017, 1, 1)
END = date(2018, 8, 31)
BLACK_FRIDAY = date(2017, 11, 24)
STRIKE = (date(2018, 5, 21), date(2018, 5, 31))
# Like the real dataset, a few stray orders after the main period ends.
TAIL = (date(2018, 9, 1), date(2018, 10, 17))


class _Ids:
    def __init__(self, rng: np.random.Generator) -> None:
        self.rng = rng

    def __call__(self) -> str:
        return self.rng.bytes(16).hex()


def _pick(rng: np.random.Generator, weights: dict[str, float], size: int) -> np.ndarray:
    keys = list(weights)
    p = np.array([weights[k] for k in keys])
    return rng.choice(keys, size=size, p=p / p.sum())


def _ts(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _daily_volume(day: date, base: float) -> float:
    progress = (day - START).days / (END - START).days
    volume = base * (0.35 + 0.65 * progress)  # the marketplace grows over time
    volume *= {5: 0.85, 6: 0.75}.get(day.weekday(), 1.05)  # quieter weekends
    if day.month == 12 and day.day <= 20:
        volume *= 1.25
    if day == BLACK_FRIDAY:
        volume *= 4.5
    elif BLACK_FRIDAY < day <= BLACK_FRIDAY + timedelta(days=3):
        volume *= 1.6
    return volume


def generate(raw_dir: Path, orders_per_day: float = 60.0, seed: int = 42) -> dict[str, int]:
    """Write the nine Olist CSVs into ``raw_dir``; returns row counts per table."""
    rng = np.random.default_rng(seed)
    new_id = _Ids(rng)
    raw_dir.mkdir(parents=True, exist_ok=True)

    categories = [
        row["category_pt"]
        for row in csv.DictReader(
            (Path(__file__).resolve().parents[3] / "seeds" / "category_es.csv").open(
                encoding="utf-8"
            )
        )
    ]
    # Two categories Olist's translation table does not cover, as in the real data.
    untranslated = ["pc_gamer", "portateis_cozinha_e_preparadores_de_alimentos"]

    sellers = [
        {
            "seller_id": new_id(),
            "seller_zip_code_prefix": f"{rng.integers(1000, 99999):05d}",
            "seller_state": st,
        }
        for st in _pick(rng, SELLER_STATES, 400)
    ]
    for s in sellers:
        s["seller_city"] = rng.choice(CITIES.get(s["seller_state"], ["capital"]))

    products = []
    cat_weights = rng.dirichlet(np.full(len(categories), 0.6))
    for _ in range(2500):
        cat = rng.choice(categories, p=cat_weights)
        if rng.random() < 0.015:
            cat = ""  # missing category
        products.append(
            {
                "product_id": new_id(),
                "product_category_name": cat,
                "product_name_lenght": int(rng.integers(10, 70)),
                "product_description_lenght": int(rng.integers(50, 3000)),
                "product_photos_qty": int(rng.integers(1, 8)),
                "product_weight_g": int(rng.lognormal(6.5, 1.1)),
                "product_length_cm": int(rng.integers(10, 80)),
                "product_height_cm": int(rng.integers(2, 60)),
                "product_width_cm": int(rng.integers(8, 60)),
                "_price": round(float(rng.lognormal(4.4, 0.8)) + 5, 2),
            }
        )
    seller_weights = rng.pareto(1.2, len(sellers)) + 0.1
    seller_weights /= seller_weights.sum()

    orders, items, payments, reviews, customers = [], [], [], [], []
    unique_customers: list[tuple[str, str]] = []
    review_ids: list[str] = []

    days = [START + timedelta(d) for d in range((END - START).days + 1)]
    tail_days = [TAIL[0] + timedelta(d) for d in range((TAIL[1] - TAIL[0]).days + 1)]
    plan = [(d, rng.poisson(_daily_volume(d, orders_per_day))) for d in days]
    plan += [(d, int(rng.random() < 0.15)) for d in tail_days]
    data_cutoff = datetime(2018, 9, 3)

    for day, n in plan:
        for _ in range(n):
            purchased = datetime(day.year, day.month, day.day) + timedelta(
                seconds=int(rng.integers(7 * 3600, 23 * 3600 + 59 * 60))
            )
            # ~3% of orders come from a returning customer.
            if unique_customers and rng.random() < 0.03:
                unique_id, state = unique_customers[rng.integers(len(unique_customers))]
            else:
                unique_id, state = new_id(), str(_pick(rng, CUSTOMER_STATES, 1)[0])
                unique_customers.append((unique_id, state))
            customer_id = new_id()
            customers.append(
                {
                    "customer_id": customer_id,
                    "customer_unique_id": unique_id,
                    "customer_zip_code_prefix": f"{rng.integers(1000, 99999):05d}",
                    "customer_city": str(rng.choice(CITIES.get(state, ["capital"]))).title()
                    if rng.random() < 0.1
                    else str(rng.choice(CITIES.get(state, ["capital"]))),
                    "customer_state": state,
                }
            )

            order_id = new_id()
            approved = purchased + timedelta(minutes=float(rng.gamma(2.0, 300)))
            slow = state in SLOW_STATES
            estimated = (
                purchased + timedelta(days=int(rng.integers(22, 32) + (8 if slow else 0)))
            ).date()
            shipped = approved + timedelta(hours=float(rng.gamma(2.0, 30)))
            transit = float(rng.gamma(4.0, 4.5 if slow else 2.2))
            if (
                STRIKE[0] <= shipped.date() <= STRIKE[1]
                or STRIKE[0] <= (shipped + timedelta(days=transit)).date() <= STRIKE[1]
            ):
                transit += float(rng.uniform(10, 20))  # trucks stopped
            delivered = shipped + timedelta(days=transit)

            r = rng.random()
            status = "delivered"
            if r < 0.006:
                status, shipped, delivered = "canceled", None, None
            elif r < 0.012:
                status, approved, shipped, delivered = "unavailable", approved, None, None
            if delivered and delivered > data_cutoff:
                status, delivered = (
                    ("shipped", None)
                    if shipped and shipped <= data_cutoff
                    else ("processing", None)
                )
                if status == "processing":
                    shipped = None
            # Deliberate quality problems.
            q = rng.random()
            if status == "delivered" and q < 0.003:
                delivered = None  # delivered without a delivery date
            elif status == "delivered" and q < 0.004:
                delivered = purchased - timedelta(days=2)  # delivered before purchase

            orders.append(
                {
                    "order_id": order_id,
                    "customer_id": customer_id,
                    "order_status": status,
                    "order_purchase_timestamp": _ts(purchased),
                    "order_approved_at": _ts(approved),
                    "order_delivered_carrier_date": _ts(shipped),
                    "order_delivered_customer_date": _ts(delivered),
                    "order_estimated_delivery_date": f"{estimated} 00:00:00",
                }
            )

            n_items = int(rng.choice([1, 2, 3, 4], p=[0.86, 0.1, 0.03, 0.01]))
            product = products[int(rng.integers(len(products)))]
            seller = sellers[int(rng.choice(len(sellers), p=seller_weights))]
            total = 0.0
            for seq in range(1, n_items + 1):
                freight = round(
                    8
                    + product["product_weight_g"] / 1000 * 2.5
                    + (9 if slow else 0)
                    + float(rng.gamma(2, 3)),
                    2,
                )
                items.append(
                    {
                        "order_id": order_id,
                        "order_item_id": seq,
                        "product_id": product["product_id"],
                        "seller_id": seller["seller_id"],
                        "shipping_limit_date": _ts(approved + timedelta(days=6)),
                        "price": f"{product['_price']:.2f}",
                        "freight_value": f"{freight:.2f}",
                    }
                )
                total += product["_price"] + freight

            ptype = str(rng.choice(PAYMENT_TYPES, p=PAYMENT_WEIGHTS))
            if rng.random() < 0.005:
                total += float(rng.uniform(5, 40))  # payment that does not reconcile
            if ptype == "voucher" and total > 50:
                voucher = round(min(total * 0.3, 50), 2)
                payments.append(
                    {
                        "order_id": order_id,
                        "payment_sequential": 1,
                        "payment_type": "voucher",
                        "payment_installments": 1,
                        "payment_value": f"{voucher:.2f}",
                    }
                )
                payments.append(
                    {
                        "order_id": order_id,
                        "payment_sequential": 2,
                        "payment_type": "credit_card",
                        "payment_installments": int(rng.integers(1, 6)),
                        "payment_value": f"{total - voucher:.2f}",
                    }
                )
            else:
                installments = int(rng.integers(1, 11)) if ptype == "credit_card" else 1
                payments.append(
                    {
                        "order_id": order_id,
                        "payment_sequential": 1,
                        "payment_type": ptype,
                        "payment_installments": installments,
                        "payment_value": f"{total:.2f}",
                    }
                )

            if status in ("delivered", "canceled", "unavailable") and rng.random() < 0.99:
                late = delivered is not None and delivered.date() > estimated
                failed = status != "delivered" or delivered is None
                if failed:
                    probs = [0.6, 0.15, 0.1, 0.07, 0.08]
                elif late:
                    probs = [0.45, 0.17, 0.15, 0.11, 0.12]
                else:
                    probs = [0.07, 0.03, 0.08, 0.2, 0.62]
                score = int(rng.choice([1, 2, 3, 4, 5], p=probs))
                created = (delivered or (purchased + timedelta(days=25))) + timedelta(days=1)
                # Like Olist, a few review_ids repeat across orders.
                review_id = review_ids[-1] if review_ids and rng.random() < 0.002 else new_id()
                review_ids.append(review_id)
                has_comment = rng.random() < 0.4
                reviews.append(
                    {
                        "review_id": review_id,
                        "order_id": order_id,
                        "review_score": score,
                        "review_comment_title": "",
                        "review_comment_message": (
                            "Produto chegou atrasado" if late else "Recomendo"
                        )
                        if has_comment
                        else "",
                        "review_creation_date": created.strftime("%Y-%m-%d 00:00:00"),
                        "review_answer_timestamp": _ts(
                            created + timedelta(hours=float(rng.gamma(2, 24)))
                        ),
                    }
                )

    translation = [
        {"product_category_name": c, "product_category_name_english": c.replace("_", " ")}
        for c in categories
        if c not in untranslated
    ]
    geolocation = []
    for st, (lat, lng) in STATE_CENTROIDS.items():
        for _ in range(20):
            geolocation.append(
                {
                    "geolocation_zip_code_prefix": f"{rng.integers(1000, 99999):05d}",
                    "geolocation_lat": round(lat + float(rng.normal(0, 1.2)), 6),
                    "geolocation_lng": round(lng + float(rng.normal(0, 1.2)), 6),
                    "geolocation_city": CITIES.get(st, ["capital"])[0],
                    "geolocation_state": st,
                }
            )

    tables = {
        "orders": orders,
        "order_items": items,
        "order_payments": payments,
        "order_reviews": reviews,
        "customers": customers,
        "sellers": sellers,
        "products": products,
        "category_translation": translation,
        "geolocation": geolocation,
    }
    counts = {}
    for table, rows in tables.items():
        file_name, columns = FILES[table]
        with (raw_dir / file_name).open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        counts[table] = len(rows)
    (raw_dir / "SYNTHETIC_DATA.txt").write_text(
        "Synthetic data generated by `bi sample` in the Olist schema. Not real Olist data.\n",
        encoding="utf-8",
    )
    return counts
