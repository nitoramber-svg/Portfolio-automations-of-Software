# KPIs, metas y seguridad por rol

> Paso 3 de 6. Todos los números salen de `bi kpis` sobre el dataset real; los comandos están
> junto a cada tabla.

## El motor

Los 13 KPIs del diseño ([design.md §5](design.md#5-kpis)) viven en
[`config/kpis.yaml`](../config/kpis.yaml): fórmula SQL, unidad, dirección (más alto o más bajo
es mejor) y meta. **Agregar un KPI no requiere tocar código.**

Cada KPI se evalúa sobre uno de tres datasets ([`sql/marts/40_datasets.sql`](../sql/marts/40_datasets.sql)):
`v_sales` (una fila por línea de pedido), `v_orders` (por pedido) y `v_reviews` (por pedido
reseñado, su última reseña). Los tres se fechan por la fecha de **compra**, así que un filtro de
fechas significa lo mismo para ventas, entregas y reseñas.

Las razones se calculan como Σ/Σ sobre las filas seleccionadas, nunca como promedio de
promedios: la puntualidad del Nordeste y la nacional salen de la misma fórmula.

```bash
bi kpis                                                   # todo, como dirección
bi kpis --user gerente.nordeste --from 2018-01-01 --currency MXN
bi kpis --by region --kpi gmv --kpi on_time_delivery      # también: month, state, payment_type, category, seller
bi kpis --filter "category=Blancos (cama, mesa y baño)"
```

### Todo el periodo (sep 2016 – oct 2018), como dirección

| KPI | Valor | Meta | |
|---|---:|---:|---|
| Ventas (GMV) | R$ 13,493,152 | | |
| Cumplimiento de meta | 104.7 % | ≥ 90 % | ✓ |
| Pedidos | 98,199 | | |
| Ticket promedio | R$ 137.42 | | |
| Clientes únicos | 94,982 | | |
| Tasa de recompra | 3.0 % | | |
| Entregas a tiempo (OTD) | 93.2 % | ≥ 90 % | ✓ |
| Días de entrega | 12.5 | | |
| Flete % de venta | 16.6 % | ≤ 20 % | ✓ |
| Tasa de cancelación | 1.2 % | ≤ 2 % | ✓ |
| Calificación promedio | 4.09 | ≥ 4.0 | ✓ |
| % reseñas negativas | 14.7 % | ≤ 15 % | ✓ |
| Vendedores activos | 3,053 | | |

El promedio esconde lo interesante. Por mes en 2018 (`bi kpis --from 2018-01-01 --to 2018-08-31 --by month`):

| Mes | Cumplimiento | OTD | Calificación |
|---|---:|---:|---:|
| ene | 112 % ✓ | 94.3 % ✓ | 4.04 ✓ |
| **feb** | 89 % ✗ | **85.9 %** ✗ | **3.83** ✗ |
| **mar** | 111 % ✓ | **81.0 %** ✗ | **3.75** ✗ |
| abr | 103 % ✓ | 95.5 % ✓ | 4.16 ✓ |
| may | 101 % ✓ | 93.4 % ✓ | 4.19 ✓ |
| jun | 83 % ✗ | 98.8 % ✓ | 4.28 ✓ |
| jul | 88 % ✗ | 96.6 % ✓ | 4.26 ✓ |
| ago | 89 % ✗ | 93.8 % ✓ | 4.26 ✓ |

En febrero y marzo la puntualidad se cae y la calificación la sigue. Las ventas de junio a agosto
quedan bajo el plan. Explicar esto es trabajo de los pasos 5 y 5b (anomalías, calendario de
eventos y la relación retraso → calificación).

Por región en 2018, la meta se cumple en todas (95–97 %), pero la **puntualidad del Nordeste
(85.6 %) y del Norte (87.6 %) está bajo la meta** mientras el Sudeste está en 93 %.

## Metas: lo que cambió al medir

Olist no publica metas. El diseño proponía «mismo mes del año anterior × (1 + crecimiento)».
Medido: Olist creció **~8 veces de enero 2017 a enero 2018** (R$ 120 mil → R$ 945 mil), así que
esa meta se cumpliría al 650 % y no serviría para nada.

La meta es un **ritmo reciente** ([`config/targets.yaml`](../config/targets.yaml)):

> meta (región, mes) = promedio de ventas de los 3 meses anteriores × (1 + 5 %)

Solo existe cuando el mes y sus 3 meses previos están **completos** (la marca de calidad del paso
2), así que hay metas de abril 2017 a agosto 2018, por cada una de las 5 regiones (85 en total).
Las metas explícitas en `overrides` ganan sobre las derivadas.

**Cumplimiento** = ventas en los días que tienen meta ÷ meta prorrateada a esos días. Si el
periodo empieza a mitad de mes, la meta también. Las ventas de días sin meta (2016, ene–mar 2017)
no inflan el cumplimiento. La meta existe por región y mes, nada más fino: para un vendedor, una
categoría o un estado, el cumplimiento queda **vacío, no inventado**.

## Seguridad por rol

Usuarios de demostración en [`config/users.yaml`](../config/users.yaml). El filtro se aplica en
la capa de consultas ([`security`](../src/bi_kpi/security/__init__.py)): el motor de KPIs y
cualquier lectura de filas pasan por `relation()`. Un usuario que no está en el archivo no recibe
nada.

| Usuario | Ve | 2018, ventas (MXN) |
|---|---|---:|
| `direccion` | Todo | 39,856,886 |
| `analista` | Todo, sin columnas que identifican al cliente (pero sí conteos de clientes) | 39,856,886 |
| `gerente.sudeste` | Pedidos de clientes del Sudeste | 26,405,614 (66 %) |
| `gerente.nordeste` | Pedidos de clientes del Nordeste | 4,371,489 (11 %) |
| `vendedor.4869f7a5` | Sus líneas de pedido, y los pedidos y reseñas a los que pertenecen | 760,209 |

**El caso difícil: pedidos compartidos.** 1,278 pedidos tienen productos de varios vendedores.
Un vendedor ve **solo sus líneas** de esos pedidos y **nunca** el valor total del pedido ni lo
pagado, porque incluyen lo de otros. Esas columnas no están en su vista, así que tampoco puede
sumarlas con una consulta escrita a mano (hay una prueba que lo intenta).

Las pruebas de [`tests/security`](../tests/security/test_rls.py) buscan fugas. Revisan que un
gerente no reciba ninguna fila de otra región en ninguno de los tres datasets, que sus KPIs sean
idénticos a los de dirección filtrando su región, y que filtrar por otra región le devuelva cero.

`bi rls-export` escribe las reglas en el formato de *dataset de permisos* de Quick Suite
(`UserName, customer_region, seller_id`; celda vacía = todo). La migración completa a Quick
Suite es el paso 6.
