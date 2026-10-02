# Rendimiento

> Paso 6. `python scripts/benchmark.py` reproduce cada tabla. Laptop con Windows 11, Python 3.11,
> DuckDB 1.x; datos reales de Olist salvo donde dice "sintéticos".

**Objetivo del diseño:** la página principal del tablero en menos de 1 segundo.

Lo que se mide es el tiempo de las consultas detrás de cada página, **en frío** (sin la caché
del tablero), mediana de 3 corridas, para enero 2017 – agosto 2018. Con la caché, volver a una
página ya vista es instantáneo; el número que importa es la primera vez que alguien abre un
filtro.

## Antes y después: los datasets como tablas

Los tres datasets que lee el motor de KPIs (`v_sales`, `v_orders`, `v_reviews`) eran vistas: cada
consulta repetía el cruce de los hechos con cinco dimensiones. Ahora se guardan como tablas al
final de `bi load`.

```bash
python scripts/benchmark.py --compare-views
```

| Rol | Página | Vistas (antes) | Tablas (ahora) |
|---|---|---:|---:|
| Dirección | Resumen | 1.68 s | **0.60 s** |
| Dirección | Ventas | 0.71 s | 0.32 s |
| Dirección | Operación | 0.85 s | 0.53 s |
| Gerente regional | Resumen | 0.81 s | 0.34 s |
| Gerente regional | Ventas | 0.31 s | 0.18 s |
| Gerente regional | Operación | 0.33 s | 0.17 s |
| Vendedor | Resumen | 1.22 s | 0.36 s |
| Vendedor | Ventas | 0.57 s | 0.17 s |
| Vendedor | Operación | 0.66 s | 0.17 s |

Todas las páginas, para todos los roles, quedan bajo 1 segundo. El costo: **1.5 s más por
`bi load`**, que corre una vez al día.

De dónde venía el tiempo de la página principal (antes): las alertas contra la meta, 0.90 s
(calculan los KPIs con meta por mes *y* por región); la tasa de recompra, 0.25 s (cuenta pedidos
por cliente para 95 mil clientes).

Antes de esto, una optimización de Python: agrupar por vendedor tardaba **11 s** porque el motor
buscaba cada valor recorriendo la tabla completa por cada uno de los ~3,000 vendedores; con una
búsqueda directa, **0.26 s**.

## Volumen ×5 (pico tipo Black Friday sostenido)

Datos sintéticos con el esquema de Olist a 1 y 5 veces su volumen:

```bash
python scripts/benchmark.py --volume 1
python scripts/benchmark.py --volume 5
```

| | ×1 | ×5 |
|---|---:|---:|
| Pedidos | 98,236 | 490,724 |
| Líneas de pedido | 116,999 | 584,049 |
| `bi load` completo (calidad, modelo, metas, detector, alertas) | 16.7 s | **55.9 s** (×3.3) |

| Rol | Página | ×1 | ×5 |
|---|---|---:|---:|
| Dirección | Resumen | 0.67 s | **1.45 s** |
| Dirección | Ventas | 0.24 s | 0.48 s |
| Dirección | Operación | 0.36 s | **1.02 s** |
| Gerente regional | Resumen | 0.34 s | 0.44 s |
| Gerente regional | Ventas | 0.14 s | 0.21 s |
| Gerente regional | Operación | 0.15 s | 0.25 s |
| Vendedor | Resumen | 0.42 s | 0.88 s |
| Vendedor | Ventas | 0.19 s | 0.37 s |
| Vendedor | Operación | 0.27 s | 0.55 s |

La carga escala mejor que lineal (5 veces los datos, 3.3 veces el tiempo). A 5 veces el volumen,
**dos páginas de dirección pasan del segundo** la primera vez que se abren con un filtro nuevo;
las de gerentes y vendedores, no (su seguridad por fila ya reduce lo que leen). Si el volumen
creciera así, el siguiente paso sería precalcular las alertas contra la meta en `bi load` (hoy
se calculan al abrir la página) y la tasa de recompra por mes.

## Lo que no se midió

- Concurrencia (varias personas a la vez): el tablero abre una conexión de solo lectura por
  consulta, así que no se bloquean entre sí, pero no hay prueba de carga con usuarios simultáneos.
- El tiempo de pintar las gráficas en el navegador (Plotly): las capturas del README tardan
  1–2 s más en total por página.
