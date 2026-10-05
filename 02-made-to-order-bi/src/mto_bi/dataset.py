"""Put the uploaded files together: one table per template plus the invoices, checked against
each other.

Uploading a template again replaces that table and keeps the rest, so the shop can send its
production file every week without resending the quotes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

import pandas as pd

from mto_bi import cfdi, ingest
from mto_bi.ingest import ERROR, WARNING, Issue, ParsedFile
from mto_bi.schema import BY_NAME, COSTS, ORDERS, PRODUCTION, QUOTES, TEMPLATES

INVOICES = "facturas"
# Classifying a file already parses it; building the dataset reuses that parse.
parse_file = lru_cache(maxsize=64)(ingest.parse_file)
FOLIO_COLUMNS = ("folio", "folio_pedido", "folio_cotizacion")


@dataclass
class FileReport:
    file: str
    table: str
    rows_read: int
    rows_loaded: int
    rows_rejected: int


@dataclass
class Dataset:
    quotes: pd.DataFrame
    orders: pd.DataFrame
    production: pd.DataFrame
    costs: pd.DataFrame
    invoices: pd.DataFrame
    issues: list[Issue] = field(default_factory=list)
    report: list[FileReport] = field(default_factory=list)
    sources: dict[str, list[tuple[str, bytes]]] = field(default_factory=dict)

    @property
    def as_of(self) -> date | None:
        """The last day anything was recorded: what "today" means for these numbers."""
        dates = [
            self.quotes.get("fecha"),
            self.quotes.get("fecha_cierre"),
            self.orders.get("fecha_pedido"),
            self.orders.get("fecha_entrega"),
            self.production.get("fecha_inicio"),
            self.production.get("fecha_fin"),
        ]
        values = [d.max() for d in dates if d is not None and d.notna().any()]
        return max(values).date() if values else None

    def has(self, table: str) -> bool:
        return len(getattr(self, table)) > 0

    def with_files(self, files: list[tuple[str, bytes]]) -> Dataset:
        """A new dataset where each template in ``files`` replaces the one loaded before."""
        incoming = classify(files)
        sources = {k: v for k, v in self.sources.items() if k not in incoming}
        sources.update(incoming)
        return build(sources)


def classify(files: list[tuple[str, bytes]]) -> dict[str, list[tuple[str, bytes]]]:
    """Group files by the table they feed: invoices by extension, templates by their headers."""
    out: dict[str, list[tuple[str, bytes]]] = {}
    for name, content in files:
        if name.lower().endswith((".xml", ".zip")):
            out.setdefault(INVOICES, []).append((name, content))
            continue
        parsed = parse_file(name, content)
        table = parsed.template.name if parsed.template else "?"
        out.setdefault(table, []).append((name, content))
    return out


def empty() -> Dataset:
    return build({})


def build(sources: dict[str, list[tuple[str, bytes]]]) -> Dataset:
    issues: list[Issue] = []
    report: list[FileReport] = []
    tables: dict[str, pd.DataFrame] = {}

    for name, content in sources.get("?", []):
        parsed = parse_file(name, content)
        issues += parsed.issues
        report.append(FileReport(name, "No reconocido", parsed.rows_read, 0, parsed.rows_read))

    for t in TEMPLATES:
        parts: list[ParsedFile] = [parse_file(n, c) for n, c in sources.get(t.name, [])]
        for p in parts:
            issues += p.issues
            report.append(FileReport(p.name, t.label, p.rows_read, len(p.data), p.rows_rejected))
        frames = [p.data.assign(archivo=p.name).reset_index() for p in parts if len(p.data)]
        df = (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=["fila", *[c.key for c in t.columns], "archivo"])
        )
        for c in FOLIO_COLUMNS:
            if c in df.columns:
                df[c] = df[c].where(df[c].isna(), df[c].astype(str).str.strip().str.upper())
        if t.key and len(parts) > 1:
            df, dup_issues = _cross_file_duplicates(df, t)
            issues += dup_issues
        tables[t.name] = df

    inv = cfdi.read_invoices(sources.get(INVOICES, []))
    issues += inv.issues
    if sources.get(INVOICES):
        report.append(
            FileReport(
                f"{len(sources[INVOICES])} archivo(s) de facturas",
                "Facturas",
                inv.files_read,
                len(inv.invoices),
                sum(1 for i in inv.issues if i.severity == ERROR),
            )
        )

    orders = tables[ORDERS.name]
    production, costs = tables[PRODUCTION.name], tables[COSTS.name]
    production, more = _orphans(production, orders, "Producción", with_line=True)
    issues += more
    costs, more = _orphans(costs, orders, "Costos", with_line=False)
    issues += more
    issues += _unknown_quotes(orders, tables[QUOTES.name])

    return Dataset(
        quotes=tables[QUOTES.name],
        orders=orders,
        production=production,
        costs=costs,
        invoices=inv.invoices,
        issues=issues,
        report=report,
        sources=sources,
    )


def _cross_file_duplicates(df: pd.DataFrame, t) -> tuple[pd.DataFrame, list[Issue]]:
    dup = df.duplicated(list(t.key), keep="last")
    issues = [
        Issue(
            r.archivo,
            int(r.fila),
            "",
            "Repetida en otro archivo: se usó la del más reciente.",
            WARNING,
        )
        for r in df[dup].itertuples()
    ]
    return df[~dup].reset_index(drop=True), issues


def _orphans(df, orders, label, with_line):
    """Rows that point at an order that isn't in the orders file are left out."""
    if df.empty:
        return df, []
    known_orders = set(orders["folio_pedido"])
    known_lines = set(zip(orders["folio_pedido"], orders["partida"], strict=True))
    missing_order = ~df["folio_pedido"].isin(known_orders)
    if with_line:
        line = list(zip(df["folio_pedido"], df["partida"], strict=True))
        missing_line = ~missing_order & ~pd.Series([k in known_lines for k in line], index=df.index)
    else:
        has_line = df["partida"].notna()
        line = list(zip(df["folio_pedido"], df["partida"], strict=True))
        missing_line = (
            ~missing_order & has_line & ~pd.Series([k in known_lines for k in line], index=df.index)
        )
    issues = [
        Issue(
            r.archivo,
            int(r.fila),
            "Folio de pedido",
            f"El pedido {r.folio_pedido} no está en Pedidos: la fila no se usó.",
            WARNING,
        )
        for r in df[missing_order].itertuples()
    ] + [
        Issue(
            r.archivo,
            int(r.fila),
            "Partida",
            f"El pedido {r.folio_pedido} no tiene partida {r.partida}: la fila no se usó.",
            WARNING,
        )
        for r in df[missing_line].itertuples()
    ]
    if issues and len(orders) == 0:
        issues = [
            Issue(
                label,
                None,
                "",
                f"{label} se cargó, pero falta la plantilla de Pedidos para relacionarla.",
                WARNING,
            )
        ]
    return df[~(missing_order | missing_line)].reset_index(drop=True), issues


def _unknown_quotes(orders, quotes) -> list[Issue]:
    if orders.empty or quotes.empty:
        return []
    linked = orders["folio_cotizacion"].dropna()
    unknown = sorted(set(linked) - set(quotes["folio"]))
    if not unknown:
        return []
    sample = ", ".join(unknown[:5]) + ("…" if len(unknown) > 5 else "")
    return [
        Issue(
            "Pedidos",
            None,
            "Folio de cotización",
            f"{len(unknown)} pedidos citan cotizaciones que no están cargadas ({sample}). "
            "Se usan igual; solo no se pueden ligar a su cotización.",
            WARNING,
        )
    ]


def table_label(name: str) -> str:
    return "Facturas" if name == INVOICES else BY_NAME[name].label
