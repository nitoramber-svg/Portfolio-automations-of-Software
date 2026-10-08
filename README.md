# Portfolio — Automations of Software

Software built to automate the real requirements of job postings.
Each folder is an independent project: it takes a role's day-to-day tasks and turns the repetitive parts into tooling.

*Software construido para automatizar los requisitos reales de vacantes de empleo. Cada carpeta es un proyecto independiente.*

| # | Project | Role it targets | Status |
|---|---|---|---|
| 01 | [BI & KPI Analytics](01-bi-kpi-analytics/) | BI / Data Analyst (Amazon Quick Suite, SQL, AWS) | ✅ Done |
| 02 | [Made-to-Order BI](02-made-to-order-bi/) | BI Analyst for a custom furniture manufacturer | ✅ Done |
| 03 | [Sentinela Ductos](03-pipeline-theft-detection/) | Industrial software / SCADA data engineer (oil & gas) | ✅ Done |

---

## 01 · [BI & KPI Analytics](01-bi-kpi-analytics/)

An end-to-end BI system on **real data from Olist**, a Brazilian marketplace (~100k orders,
2016–2018), built requirement by requirement from a BI Analyst job posting.

![Executive summary](01-bi-kpi-analytics/docs/screenshots/01-resumen.png)

**What it offers**

- **Anomaly alerts that see trouble coming.** It flagged the May 2018 national truckers' strike
  **7 days before deliveries failed** — and each alert arrives with who acts, what to do and the
  list of orders at risk.
- **A dashboard for each role.** Directors see everything; regional managers, only their region;
  sellers, only their own sales — enforced in the query layer, with leak tests.
- **Numbers you can trust.** 30 data-quality checks, a quarantine for contradictory rows, and a
  reconciliation that refuses to publish if a single cent was lost.
- **KPIs in plain language.** 13 KPIs declared in configuration, in BRL / MXN / USD, against a
  sales plan — and a written reading of what happened for non-technical readers.
- **Ready for the cloud.** One command exports Parquet, Athena tables and QuickSight row-level
  security rules; every page loads in under a second.

| Anomaly alerts, with events and orders at risk | What each event cost |
|---|---|
| ![Alerts](01-bi-kpi-analytics/docs/screenshots/04-alertas.png) | ![Events](01-bi-kpi-analytics/docs/screenshots/08-eventos.png) |
| **Operations & satisfaction** | **The same dashboard, as a regional manager** |
| ![Operations](01-bi-kpi-analytics/docs/screenshots/03-operacion.png) | ![Regional manager](01-bi-kpi-analytics/docs/screenshots/06-gerente-nordeste.png) |

Python · SQL · DuckDB · Streamlit · Plotly · pytest (127 tests) · GitHub Actions ·
[Read more →](01-bi-kpi-analytics/)

---

## 02 · [Made-to-Order BI](02-made-to-order-bi/)

A web dashboard a small custom furniture workshop can run **without anyone technical in the
middle**: each area fills in an Excel template, administration drops in the SAT's invoice XML,
and everyone gets their own board. Built for a real furniture maker in Mexico; this repository
runs on a made-up workshop.

![Summary](02-made-to-order-bi/docs/screenshots/01-resumen.png)

**What it offers**

- **Orders that will arrive late, before they do.** For each open piece: the stages it still
  needs at the shop's recent pace, an estimated date against the promise, and the reason. Checked
  against history: 72 % of the pieces it flags were late (guessing: 26 %), dates off by 2 days.
- **Excel in, no IT needed.** Templates with drop-down lists; Mexican formats understood; every
  mistake reported with its Excel row, the rest still loads.
- **SAT invoices as they come.** CFDI 3.3 and 4.0, loose or in the bulk-download ZIP: billed vs
  delivered, orders never invoiced, invoices with no order.
- **Close rate, shop-floor times and margin by piece and brand**, with a plain-language reading
  that says which margin fell and which cost explains it.
- **Each area sees its own.** Sales never sees costs; the shop never sees prices.

| Orders at risk, with the reason for each | Upload: every mistake with its row |
|---|---|
| ![Orders at risk](02-made-to-order-bi/docs/screenshots/04-riesgo.png) | ![Upload](02-made-to-order-bi/docs/screenshots/07-cargar.png) |

Python · pandas · Streamlit · Plotly · openpyxl · CFDI XML · pytest (83 tests) · GitHub Actions ·
[Read more →](02-made-to-order-bi/)

---

## 03 · [Sentinela Ductos](03-pipeline-theft-detection/)

Pemex lost **MXN 23.5 billion to fuel theft in 2025**: one illegal tap every 51 minutes, and the
tool that finds them takes about 30 days. Sentinela reads the pressure and flow a SCADA already
has and, **within minutes**, says there is a tap, at which km, which access road and brigade, and
how many litres are being stolen. Runs on a simulated pipeline.

![Dashboard](03-pipeline-theft-detection/docs/screenshots/01-tablero.jpg)

**What it offers**

- **A tap found in ~2.3 min and placed within a few hundred metres**, from the pressure wave the
  drilling sends down the line; slow-opening taps caught by the flow balance and the pressure profile.
- **No crying wolf.** Pump maneuvers (even unlogged ones) and failing transmitters are told apart:
  0 false alarms in 2 simulated days; 100 % detection from 5 m³/h in a 160-tap Monte Carlo campaign.
- **Live SCADA data as it really arrives**: HTTP and encrypted OPC-UA, with late, lost and junk
  readings handled so a data gap is never read as a tap.
- **Ready to run 24/7**: roles and audit trail, HTTPS, hot backups, restart without losing the
  shift, and a watchdog that pages the on-call team on Telegram.

| Incidents, each step signed by who did it | SCADA link and data quality |
|---|---|
| ![Incidents](03-pipeline-theft-detection/docs/screenshots/02-incidentes.jpg) | ![Data quality](03-pipeline-theft-detection/docs/screenshots/03-calidad-scada.jpg) |

Python (standard library) · SQLite · OPC-UA · vanilla JS · 37 tests · GitHub Actions ·
[Read more →](03-pipeline-theft-detection/)

---

## Also see

- [GovIntel MX](https://github.com/nitoramber-svg/GovIntel-MX) — procurement intelligence platform over 520k+ Mexican public contracts (FastAPI, PostgreSQL, Celery, Docker).

## License

[MIT](LICENSE)
