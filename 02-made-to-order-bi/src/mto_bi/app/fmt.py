"""Numbers and dates the way people in a Mexican shop read them."""

from __future__ import annotations

import math

import pandas as pd

MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT


def money(v, compact: bool = True) -> str:
    if missing(v):
        return "—"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if compact and v >= 1_000_000:
        return f"{sign}${v / 1_000_000:,.1f} M"
    if compact and v >= 10_000:
        return f"{sign}${v / 1_000:,.0f} mil"
    return f"{sign}${v:,.0f}"


def pct(v, digits: int = 0) -> str:
    return "—" if missing(v) else f"{v:.{digits}%}"


def num(v) -> str:
    return "—" if missing(v) else f"{v:,.0f}"


def days(v) -> str:
    if missing(v):
        return "—"
    return f"{v:,.0f} día" + ("" if round(v) == 1 else "s")


def day(d) -> str:
    if missing(d):
        return "—"
    d = pd.Timestamp(d)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def month(d) -> str:
    d = pd.Timestamp(d)
    return f"{MONTHS[d.month - 1]} {d.year}"


def md(text: str) -> str:
    """Streamlit reads $…$ as math: escape the peso sign."""
    return str(text).replace("$", r"\$")
