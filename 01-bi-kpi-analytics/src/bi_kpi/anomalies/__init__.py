"""Anomaly detection: each KPI x region series against its own recent past.

Every series is dated by the day its value becomes *knowable*, so a replay day by day sees
exactly what a live system would have seen:

    orders, gmv        purchase date
    on_time_delivery   estimated delivery date: of the orders due that day, the share already
                       delivered (Olist only promises business days, so this series has no
                       weekends or holidays)
    negative_reviews   review date, by week: reviews written on Sundays are 28 % negative and
                       on Mondays there are almost none, so days are not comparable

Method (design §6): robust z-score = (value - median) / (1.4826 * MAD) over the previous N
observations — observations, not calendar days, because the on-time series skips weekends. Counts
are first divided by a weekday factor from the previous 8 weeks. |z| > 3.5 is an anomaly. A
group whose volume is too thin day to day (Norte) is evaluated by week instead.

Nothing is flagged on incomplete data: incomplete months (step 2) and the dataset's tail, where
volume fades out as the snapshot ends (``data_cutoff``), are reported once as "incomplete", not
as a drop.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

THRESHOLD = 3.5
CRITICAL = 6.0
MIN_VOLUME = 30  # a period with fewer orders / reviews than this is not judged
NATIONAL = "Brasil"


@dataclass(frozen=True)
class Series:
    id: str
    name: str
    kind: str  # "count" (sum of value) or "ratio" (numerator / denominator)
    dow_adjust: bool
    grain: str  # "day" or "week"
    window: int  # observations in the baseline
    unit: str  # for messages: count | currency | ratio
    alert_on: str  # "both", "up" or "down": the direction that is bad news
    sql: str  # date, region, num, den  (den = volume; for counts den = orders)


SERIES: tuple[Series, ...] = (
    Series(
        "orders",
        "Pedidos",
        "count",
        True,
        "day",
        28,
        "count",
        "both",
        """SELECT CAST(o.purchased_at AS DATE) AS date, c.region,
                  count(*) AS num, count(*) AS den
           FROM mart.fact_orders o JOIN mart.dim_customer c USING (customer_key)
           WHERE NOT o.is_canceled GROUP BY ALL""",
    ),
    # By week: one R$ 10,000 order moves a regional day by 4 sigma.
    Series(
        "gmv",
        "Ventas",
        "count",
        False,
        "week",
        8,
        "currency",
        "both",
        """SELECT CAST(o.purchased_at AS DATE) AS date, c.region,
                  sum(o.items_brl) AS num, count(*) AS den
           FROM mart.fact_orders o JOIN mart.dim_customer c USING (customer_key)
           WHERE NOT o.is_canceled GROUP BY ALL""",
    ),
    Series(
        "on_time_delivery",
        "Entregas a tiempo",
        "ratio",
        False,
        "day",
        20,
        "ratio",
        "down",
        """SELECT o.estimated_delivery AS date, c.region,
                  count(*) FILTER (WHERE CAST(o.delivered_at AS DATE) <= o.estimated_delivery)
                      AS num,
                  count(*) AS den
           FROM mart.fact_orders o JOIN mart.dim_customer c USING (customer_key)
           WHERE NOT o.is_canceled AND o.estimated_delivery IS NOT NULL GROUP BY ALL""",
    ),
    Series(
        "negative_reviews",
        "Reseñas negativas",
        "ratio",
        False,
        "week",
        8,
        "ratio",
        "up",
        """SELECT CAST(strptime(CAST(r.created_date_key AS VARCHAR), '%Y%m%d') AS DATE) AS date,
                  c.region, count(*) FILTER (WHERE r.is_negative) AS num, count(*) AS den
           FROM mart.fact_reviews r
           JOIN mart.fact_orders o USING (order_id)
           JOIN mart.dim_customer c USING (customer_key)
           WHERE r.is_latest_for_order GROUP BY ALL""",
    ),
)
SERIES_BY_ID = {s.id: s for s in SERIES}


def data_cutoff(daily_orders: pd.Series) -> pd.Timestamp | None:
    """Last day the data is complete, or None.

    The cutoff is the day before the first day D from which *every* later day stays under half
    the median of the 28 days before D. Olist's snapshot fades out this way at the end of
    August 2018; comparing each tail day with its own recent past would not see it, because the
    recent past is the fading tail itself.
    """
    x = daily_orders.asfreq("D", fill_value=0).astype(float)
    base = x.shift(1).rolling(28, min_periods=21).median().to_numpy()
    tail_max = x[::-1].cummax()[::-1].to_numpy()  # max of this day and every later day
    for i in range(len(x)):
        if not np.isnan(base[i]) and base[i] > 0 and tail_max[i] < 0.5 * base[i]:
            return x.index[i - 1]
    return None


def _weekly(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = df["date"] - pd.to_timedelta(df["date"].dt.dayofweek, unit="D")
    return df.groupby(["series", "region", "date"], as_index=False)[["num", "den"]].sum()


def build_series(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Long table: series, region (incl. Brasil), grain, date, num, den, value."""
    parts = []
    for s in SERIES:
        df = con.execute(s.sql).df()
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df["series"] = s.id
        df = df.dropna(subset=["region"])
        national = df.groupby(["series", "date"], as_index=False)[["num", "den"]].sum()
        national["region"] = NATIONAL
        df = pd.concat([df[["series", "region", "date", "num", "den"]], national])
        for region, g in df.groupby("region"):
            daily_volume = g.groupby("date")["den"].sum().median()
            grain = "week" if s.grain == "week" or daily_volume < MIN_VOLUME else "day"
            g = _weekly(g) if grain == "week" else g
            parts.append(g.assign(grain=grain, region=region))
    out = pd.concat(parts, ignore_index=True)
    out["num"] = out["num"].astype(float)
    out["den"] = out["den"].astype(float)
    kinds = out["series"].map(lambda i: SERIES_BY_ID[i].kind)
    out["value"] = np.where(kinds == "ratio", out["num"] / out["den"], out["num"])
    return out.sort_values(["series", "region", "date"]).reset_index(drop=True)


def _robust_z(
    values: np.ndarray, window: int, floor: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """z, expected value and scale of each point against the previous ``window`` points."""
    n = len(values)
    z = np.full(n, np.nan)
    expected = np.full(n, np.nan)
    scales = np.full(n, np.nan)
    min_obs = int(window * 0.75)
    for i in range(n):
        past = values[max(0, i - window) : i]
        past = past[~np.isnan(past)]
        if len(past) < min_obs or np.isnan(values[i]):
            continue
        med = np.median(past)
        mad = np.median(np.abs(past - med)) * 1.4826
        scale = max(mad, floor if floor > 0 else 0, abs(med) * 0.01)
        if scale == 0:
            continue
        z[i] = (values[i] - med) / scale
        expected[i] = med
        scales[i] = scale
    return z, expected, scales


def _weekday_factor(g: pd.DataFrame) -> np.ndarray:
    """Per point: median of the same weekday / median of all, over the previous 8 weeks."""
    v = g["value"].to_numpy(dtype=float)
    dates = g["date"].to_numpy()
    dows = g["date"].dt.dayofweek.to_numpy()
    f = np.full(len(v), np.nan)
    for i in range(len(v)):
        lo = dates[i] - np.timedelta64(56, "D")
        mask = (dates < dates[i]) & (dates >= lo)
        past, past_dow = v[mask], dows[mask]
        same = past[past_dow == dows[i]]
        if len(past) >= 28 and len(same) >= 4 and np.median(past) > 0:
            f[i] = np.median(same) / np.median(past)
    return f


def detect(series: pd.DataFrame, complete: pd.Series) -> pd.DataFrame:
    """Add expected, z and status to every point. ``complete``: date -> bool (data usable).

    status: anomaly_up / anomaly_down / normal / thin (volume < MIN_VOLUME) / incomplete
    """
    out = []
    for (sid, _region), g in series.groupby(["series", "region"], sort=False):
        s = SERIES_BY_ID[sid]
        g = g.sort_values("date").copy()
        week = g["grain"].iloc[0] == "week"
        usable = g["date"].map(
            lambda d, week=week: (
                bool(complete.get(d, False))
                and (not week or bool(complete.get(d + pd.Timedelta(days=6), False)))
            )
        )
        thin = g["den"] < MIN_VOLUME
        values = g["value"].to_numpy(dtype=float).copy()
        values[(~usable | thin).to_numpy()] = np.nan
        factor = _weekday_factor(g) if s.dow_adjust and not week else np.ones(len(g))
        # Until 8 weeks of history give a weekday factor, a seasonal series is not judged: an
        # ordinary quiet Sunday would read as a collapse.
        adjusted = values / factor
        floor = 0.005 if s.kind == "ratio" else 1.0
        window = s.window if not week or s.grain == "week" else 8
        z, expected, scale = _robust_z(adjusted, window, floor)
        g["expected"] = expected * factor
        # Band of normal values for the charts: expected ± THRESHOLD scales, in raw units.
        g["low"] = (expected - THRESHOLD * scale) * factor
        g["high"] = (expected + THRESHOLD * scale) * factor
        g["z"] = z
        g["status"] = np.select(
            [~usable.to_numpy(), thin.to_numpy(), z > THRESHOLD, z < -THRESHOLD],
            ["incomplete", "thin", "anomaly_up", "anomaly_down"],
            default="normal",
        )
        out.append(g)
    return pd.concat(out, ignore_index=True)


def episodes(detections: pd.DataFrame) -> pd.DataFrame:
    """Consecutive anomalous points of one series, region and direction = one alert.

    A gap of one normal point does not split an episode (a strike doesn't pause on Tuesday).
    The alert is raised on the first point; peak and end are filled in as the episode goes on.
    """
    rows = []
    anomalous = detections[detections["status"].str.startswith("anomaly")]
    for (sid, region, status), g in anomalous.groupby(["series", "region", "status"]):
        allpts = (
            detections[(detections["series"] == sid) & (detections["region"] == region)]
            .sort_values("date")
            .reset_index(drop=True)
        )
        idx = allpts.index[allpts["date"].isin(g["date"])].to_list()
        groups, current = [], [idx[0]]
        for i in idx[1:]:
            if i - current[-1] <= 2:
                current.append(i)
            else:
                groups.append(current)
                current = [i]
        groups.append(current)
        for grp in groups:
            pts = allpts.loc[grp]
            first = pts.iloc[0]
            peak = pts.loc[pts["z"].abs().idxmax()]
            rows.append(
                {
                    "series": sid,
                    "region": region,
                    "direction": "up" if status == "anomaly_up" else "down",
                    "grain": first["grain"],
                    "start": first["date"],
                    "end": pts.iloc[-1]["date"],
                    "points": len(pts),
                    "value": first["value"],
                    "expected": first["expected"],
                    "z": first["z"],
                    "peak_date": peak["date"],
                    "peak_value": peak["value"],
                    "peak_expected": peak["expected"],
                    "peak_z": peak["z"],
                    "severity": "critical" if pts["z"].abs().max() >= CRITICAL else "warning",
                }
            )
    cols = [
        "series",
        "region",
        "direction",
        "grain",
        "start",
        "end",
        "points",
        "value",
        "expected",
        "z",
        "peak_date",
        "peak_value",
        "peak_expected",
        "peak_z",
        "severity",
    ]
    eps = pd.DataFrame(rows, columns=cols).sort_values(["start", "series", "region"])
    eps["alert"] = [is_alert(r) for r in eps.itertuples()]
    eps["alert_date"] = [alert_date(r, detections) for r in eps.itertuples()]
    return eps.reset_index(drop=True)


def is_alert(ep) -> bool:
    """An episode becomes an alert when it goes the bad way and is either critical or lasts
    two points. A single 4-sigma day is noted, not sent: that is what keeps false alarms at
    about one per KPI per month (docs/alerts.md)."""
    s = SERIES_BY_ID[ep.series]
    bad_way = s.alert_on in ("both", ep.direction)
    return bad_way and (ep.severity == "critical" or ep.points >= 2)


def alert_date(ep, detections: pd.DataFrame):
    """When the alert would have been sent: the first point if critical, else the second.

    A weekly point is only known when its week closes, so it is sent on that Sunday.
    """
    pts = detections[
        (detections["series"] == ep.series)
        & (detections["region"] == ep.region)
        & (detections["date"] >= ep.start)
        & (detections["date"] <= ep.end)
        & detections["status"].str.startswith("anomaly")
    ].sort_values("date")
    # Whichever comes first: the first critical point, or the second point (persistence).
    candidates = list(pts.loc[pts["z"].abs() >= CRITICAL, "date"].iloc[:1])
    if len(pts) >= 2:
        candidates.append(pts["date"].iloc[1])
    day = min(candidates) if candidates else pts["date"].iloc[0]
    return day + pd.Timedelta(days=6) if ep.grain == "week" else day


def completeness(con: duckdb.DuckDBPyConnection) -> tuple[pd.Series, pd.Timestamp | None]:
    """date -> usable, and the data cutoff. Usable = complete month and not after the cutoff."""
    cal = con.execute("SELECT date, is_complete_month FROM mart.dim_date").df()
    cal["date"] = pd.to_datetime(cal["date"])
    daily = con.execute(
        "SELECT CAST(purchased_at AS DATE) AS date, count(*) AS n FROM mart.fact_orders "
        "GROUP BY 1 ORDER BY 1"
    ).df()
    cutoff = None
    if len(daily):
        daily["date"] = pd.to_datetime(daily["date"])
        cutoff = data_cutoff(daily.set_index("date")["n"])
    usable = cal.set_index("date")["is_complete_month"].astype(bool)
    if cutoff is not None:
        usable[usable.index > cutoff] = False
    return usable, cutoff


def run(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Series, detections and episodes for the whole history, into the ``alerts`` schema."""
    usable, cutoff = completeness(con)
    series = build_series(con)
    det = detect(series, usable)
    eps = episodes(det)
    con.execute("CREATE SCHEMA IF NOT EXISTS alerts")
    con.register("_det", det)
    con.execute("CREATE OR REPLACE TABLE alerts.detections AS SELECT * FROM _det")
    con.unregister("_det")
    con.register("_eps", eps)
    con.execute("CREATE OR REPLACE TABLE alerts.episodes AS SELECT * FROM _eps")
    con.unregister("_eps")
    con.execute(
        "CREATE OR REPLACE TABLE alerts.meta AS SELECT CAST(? AS DATE) AS data_cutoff",
        [cutoff.date() if cutoff is not None else None],
    )
    return {"points": len(det), "episodes": len(eps)}
