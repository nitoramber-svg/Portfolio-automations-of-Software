# Calidad de datos — lo que se encontró en Olist

> Paso 2 de 6. Medido sobre el dataset real completo (99,441 pedidos). `bi quality` reproduce
> cada número de este documento.

El diseño ([design.md §7](design.md#7-calidad-y-gobernanza-de-datos)) enumeró los problemas que
*esperábamos* encontrar. Antes de escribir una sola regla se midieron. Varios no existen, otros
eran más grandes o distintos de lo esperado, y aparecieron dos que nadie había previsto.

## Cómo funciona

Las pruebas corren **entre staging y los marts**. Cada una devuelve las filas que marca y su
severidad decide qué pasa con ellas:

| Severidad | Qué significa | Qué pasa con la fila |
|---|---|---|
| **cuarentena** | La fila se contradice a sí misma | Sale de staging a `quarantine.<tabla>` con el motivo, antes de construir los marts. Sus productos, pagos y reseñas la siguen |
| **corregido** | Staging ya lo arregló | Se queda, corregida |
| **advertencia** | Es real pero afecta a algún KPI | Se queda; el KPI que depende de ella debe saberlo |
| **informativo** | Parece error, pero así funciona el negocio | Se queda; se reporta para que nadie lo "arregle" |

Al final, una **conciliación** comprueba que lo que entró a staging = lo que llegó a los marts +
lo que está en cuarentena (pedidos, líneas y reales al centavo), y que las llaves de los hechos
son únicas. Si no cuadra, `bi load` **se niega a publicar** y termina con error.

El reporte queda en `quality.report` (para el dashboard) y en `bi quality`.

## Resultados con los datos reales

### Cuarentena — 14 pedidos de 99,441 (R$ 2,127)

| Prueba | Filas | Por qué no se puede usar |
|---|---:|---|
| «Entregado» sin fecha de entrega | 8 | No se puede medir si llegó a tiempo y no se sabe si llegó |
| Cancelado pero con fecha de entrega | 6 | No se sabe si fue venta o no |

Con ellos salen sus 15 productos, 14 pagos y 14 reseñas. La conciliación:
R$ 13,591,643.70 en staging = marts + cuarentena.

**Lo que se esperaba y no existe:** entregas antes de la compra (0), aprobaciones antes de la
compra (0), fechas ilegibles (0), productos de pedidos inexistentes (0), precios negativos (0),
pedidos duplicados (0). Las pruebas se quedan, porque cuestan nada y protegen contra un cambio
en la fuente, pero **Olist es estructuralmente limpio; sus problemas son de lógica**.

### Corregido en staging

| Prueba | Filas | Detalle |
|---|---:|---|
| Ciudad del vendedor | 61 de 3,095 (2 %) | Ver abajo |
| Categoría sin traducción oficial | 13 productos | `pc_gamer` y `portateis_cozinha…` no están en la tabla de Olist; las cubre la tabla en español |

**Ciudades.** El diseño esperaba variantes de acentos y mayúsculas («São Paulo» / «sao paulo»).
Medido: en **clientes** las ciudades ya vienen limpias (0 cambios); el problema está en los
**vendedores**, que escriben su ciudad a mano:

| Tipo | Vendedores | Ejemplos |
|---|---:|---|
| Estado pegado | 25 | «sao paulo - sp», «auriflama/sp», «brasilia df», «novo hamburgo, rio grande do sul, brasil» |
| Errores de dedo | 24 | «garulhos», «belo horizont», «sao paluo», «riberao preto» |
| No es una ciudad | 11 | «vendas@creditparts.com.br», «04482255», «sp», «parana» |

Reglas, en orden: (1) cortar lo que sigue a «/», «,», « - » o «(», y un código de estado al final;
(2) si es puro número, un correo, un código de estado o el nombre de **su propio** estado,
sustituirlo por la ciudad más común de su código postal; (3) si no es una ciudad conocida de su
estado pero se parece ≥ 0.9 (Jaro-Winkler) a la de su código postal, tomar esa.

Dos versiones anteriores de estas reglas **borraban datos buenos**, y las pruebas lo detectaron:
- «sao paulo» y «rio de janeiro» son nombres de estado *y* de ciudad: solo se reemplaza el nombre
  del estado propio, y solo si no es también ciudad de ese estado.
- «quilometro 14 do mutum» es un lugar real con un número: solo cuenta como basura lo que es
  *puro* número.

Los nombres de distritos que no aparecen en la tabla de geolocalización («colonia jordaozinho»)
se dejan como están: sustituirlos por la ciudad del código postal sería inventar.

### Advertencias

| Prueba | Filas | Qué hacer con ella |
|---|---:|---|
| Enviado antes de aprobar el pago | 1,359 (1.4 %) | Mediana 17 h antes. No usar esos pedidos para «tiempo de preparación» |
| Pedido con varias reseñas | 547 pedidos | El cliente volvió a contestar (202 con calificación distinta). `fact_reviews.is_latest_for_order` marca la última |
| Producto sin categoría | 610 (1.9 %) | Aparece como «Sin categoría» |
| Enviado antes de la compra | 166 | **Todos entre abril y agosto de 2018**, mediana 35 min antes: reloj del sistema del transportista. El tiempo total de entrega sí es válido |
| **Estado del vendedor ≠ estado de su CP** | 35 (1.1 %) | No estaba previsto. Ej.: vendedores con CP de Río (21xxx) declarados en SP. Afecta los análisis por región *del vendedor*; la seguridad por región (paso 3) filtra por la del cliente, así que no la toca |
| Pago ≠ precio + flete sin explicación | 54 | 39 pagan *menos* (todos entregados) |
| Entregado antes de salir con la paquetería | 23 | El tiempo total es válido; el de tránsito no |
| Meses incompletos | 6 de 26 | Sep–dic 2016 (nov 2016 sin un solo pedido) y sep–oct 2018. `dim_date.is_complete_month` los marca para que tendencias y anomalías no los comparen |
| Otros | 18 | Producto sin peso (6), pedido activo sin productos (8), forma de pago «not_defined» (3), pedido sin pago (1) |

### Informativo — parece error, no lo es

| Prueba | Filas | Explicación |
|---|---:|---|
| Pagó más que precio + flete | 249 pedidos | Tarjeta a meses: ~10 % más (mediana). Son intereses |
| Una reseña para varios pedidos | 789 reseñas en 1,603 filas | Mismo cliente, mismo contenido: pedidos comprados juntos que se calificaron una vez. Por eso `fact_reviews` es por (reseña, pedido), y contar reseñas exige `count(DISTINCT review_id)` |

**La conciliación de pagos del diseño, desglosada.** «Σ pagos ≠ Σ (precio + flete)» marca 1,075
pedidos. Medido por causa: 772 son pedidos que **no tienen productos** (764 de ellos cancelados o
no disponibles: lo pagado no tiene contra qué cuadrar), 249 son intereses y solo 54 no tienen
explicación. Reportar 1,075 «errores» habría escondido los 54 que sí importan.

## Qué no se prueba (y por qué)

- **Códigos postales duplicados en geolocalización** (1,000,163 filas para 19,015 códigos): el
  diseño lo esperaba como problema, pero son varias coordenadas por código, a propósito. Se usa la
  ciudad más común de cada código; el mapa del paso 4 decidirá sus coordenadas.
- **Texto de las reseñas**: está en portugués y fuera de alcance (diseño §13).
