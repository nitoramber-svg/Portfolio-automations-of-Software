# Migración a AWS y Amazon Quick Suite (QuickSight)

> Paso 6. **Guía, no ejecutada**: el proyecto corre local y gratis a propósito; no hay cuenta de
> AWS detrás. Lo que sí está hecho y probado es la exportación (`bi export`): los archivos, el
> SQL de Athena y las reglas de seguridad que esta guía usa.

La vacante pide Amazon Quick Suite, SQL y AWS. El proyecto se construyó para que el salto sea de
infraestructura, no de lógica: el modelo es SQL estándar, los KPIs están declarados, la seguridad
por fila ya está pensada en el formato que QuickSight aplica.

## Equivalencias

| Aquí (local) | En AWS |
|---|---|
| Archivos CSV + SQLite + API de tipo de cambio | Mismas fuentes; el pipeline corre como tarea programada (ECS Fargate o AWS Glue Python shell) con EventBridge |
| DuckDB (`warehouse.duckdb`) | **S3** con Parquet + **Athena** (catálogo de Glue). El SQL de `sql/` corre en Athena casi sin cambios |
| Datasets `v_sales`, `v_orders`, `v_reviews` | **Datasets de QuickSight** importados a **SPICE**, refresco diario después de la carga |
| Motor de KPIs (`config/kpis.yaml`) | **Campos calculados** del dataset (tabla abajo) |
| Seguridad por fila (`bi_kpi.security`) | **RLS** de QuickSight con un dataset de permisos (`rls_rules.csv`) + **seguridad por columna** |
| Dashboard Streamlit | **Análisis** de QuickSight publicado como **dashboard** |
| Detector de anomalías y alertas (`bi run-daily`) | Se mantiene como tarea programada; manda correo con **SES** y Slack con un webhook. QuickSight agrega **alertas por umbral** para las reglas contra la meta |
| Bandeja `outbox/` | SES en modo sandbox para probar sin enviar a nadie real |

## Paso a paso

**1. Exportar.**

```bash
bi export --bucket s3://mi-bucket/olist-bi --out data/export
```

Escribe una carpeta por tabla con su Parquet, `athena.sql`, `rls_rules.csv` y `manifest.json`
(filas por tabla, para qué sirve y qué columnas identifican clientes). Con los datos reales: 18
tablas, 74 MB.

**2. Subir a S3.**

```bash
aws s3 sync data/export s3://mi-bucket/olist-bi --exclude "*.sql" --exclude "*.json" --exclude "*.csv"
```

**3. Crear las tablas en Athena.** Ejecutar `athena.sql` en la consola de Athena: crea la base
`olist_bi` y una tabla externa por carpeta, con los tipos traducidos de DuckDB (`DECIMAL(12,2)` →
`decimal(12,2)`, `VARCHAR[]` → `array<string>`).

**4. Datasets en QuickSight.** Desde Athena, importar a SPICE: `v_sales`, `v_orders`,
`v_reviews`, `fact_targets`, `qs_orders_by_seller` y `qs_reviews_by_seller`. Programar el
refresco diario después de la carga.

**5. Seguridad por fila.** Subir `rls_rules.csv` como dataset de permisos:

```
UserName,customer_region,seller_id
direccion,,
gerente.nordeste,Nordeste,
vendedor.4869f7a5,,4869f7a5dfa277a7dca6462dcf3b52b2
```

Una celda vacía significa "todo". Aplicarlo así:

| Dataset | Reglas por | Para |
|---|---|---|
| `v_sales` | `customer_region` y `seller_id` | Todos |
| `v_orders`, `v_reviews` | `customer_region` | Dirección, analista, gerentes |
| `qs_orders_by_seller`, `qs_reviews_by_seller` | `seller_id` | Solo vendedores |

**La diferencia con el proyecto:** aquí, un vendedor ve "los pedidos que contienen una de mis
líneas" con una subconsulta. La RLS de QuickSight solo compara columnas del propio dataset, así
que la exportación agrega `qs_orders_by_seller` y `qs_reviews_by_seller`, con una fila por pedido
y vendedor y **sin los totales del pedido** (incluyen lo de otros vendedores). Sus sumas cuentan
dos veces los pedidos compartidos: no sirven para totales de dirección, solo para la vista de cada
vendedor.

**6. Seguridad por columna.** En `v_sales`, `v_orders` y `v_reviews`, restringir
`customer_unique_id` y `customer_city` a dirección y gerentes: el analista ve todas las filas
pero no esas columnas, como aquí. El `manifest.json` lista qué columnas son de clientes en cada
tabla.

## Los KPIs como campos calculados

| KPI | Campo calculado en QuickSight |
|---|---|
| Ventas (GMV) | `sumIf(price_brl, is_canceled = false)` |
| Pedidos | `countIf(order_id, is_canceled = false)` (en `v_orders`) |
| Ticket promedio | `sumIf(price_brl, is_canceled = false) / distinct_countIf(order_id, is_canceled = false)` |
| Clientes únicos | `distinct_countIf(customer_unique_id, is_canceled = false)` |
| Tasa de recompra | Con un cálculo a nivel de cliente (LAC): `pedidos_por_cliente = countOver(order_id, [customer_unique_id], PRE_AGG)`, luego `distinct_countIf(customer_unique_id, pedidos_por_cliente >= 2) / distinct_count(customer_unique_id)` |
| Entregas a tiempo | `countIf(order_id, on_time = true) / countIf(order_id, is_delivered = true)` |
| Días de entrega | `avgIf(delivery_days, is_delivered = true)` |
| Flete % de venta | `sumIf(freight_brl, is_canceled = false) / sumIf(price_brl, is_canceled = false)` |
| Tasa de cancelación | `countIf(order_id, is_canceled = true) / count(order_id)` |
| Calificación promedio | `avg(score)` (en `v_reviews`) |
| % reseñas negativas | `countIf(review_id, is_negative = true) / count(review_id)` |
| Vendedores activos | `distinct_countIf(seller_id, is_canceled = false)` |
| Cumplimiento de meta | `sum(ventas) / sum(target_brl)` uniendo `v_sales` con `fact_targets` por mes y región |

**Moneda:** un parámetro `Moneda` y `ifelse(${Moneda} = 'MXN', price_mxn, ${Moneda} = 'USD', price_usd, price_brl)`.

**Lo que se pierde:** aquí el cumplimiento se prorratea por día (medio mes, media meta). En
QuickSight, con `fact_targets` mensual, el cumplimiento de un mes a medias se compara contra la
meta completa; para igualarlo habría que exportar la meta por día.

## Anomalías: las de QuickSight y las de aquí

QuickSight trae **detección de anomalías con aprendizaje automático** (ML Insights, Random Cut
Forest). Sirve para explorar, pero no reemplaza al detector de este proyecto, por tres razones
medidas en el paso 5:

1. Aquí cada serie se fecha **cuando su valor se conoce** (la puntualidad, por la fecha en que el
   pedido debía llegar). QuickSight detecta sobre lo que el dataset le da.
2. El **calendario de eventos**: un Black Friday planeado no alerta por su pico; QuickSight no
   sabe qué es un evento planeado.
3. Las **alertas tempranas, los playbooks y los pedidos en riesgo** viven en el detector.

Recomendación: el detector sigue como tarea programada; `alerts_log` se publica como dataset para
verlo en QuickSight; las **alertas por umbral** de QuickSight cubren las reglas simples contra la
meta (OTD < 90 %, calificación < 4.0).

## Costos y lo que falta para hacerlo de verdad

No se ejecutó. Para hacerlo: una cuenta de AWS (QuickSight tiene prueba gratuita), un bucket,
permisos de QuickSight sobre Athena y S3, y los usuarios de QuickSight con los mismos nombres que
`config/users.yaml` (o grupos, si se prefiere `GroupName` en el dataset de permisos).
