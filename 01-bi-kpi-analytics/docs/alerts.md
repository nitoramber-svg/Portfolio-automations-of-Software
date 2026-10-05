# Anomalías, alertas y eventos

> Pasos 5 y 5b de 6. Todo medido sobre el dataset real; `bi replay` y `bi impact` reproducen
> cada número.

## Qué hace

Cada noche (`bi run-daily`) el sistema compara cada KPI de cada región con su propio pasado,
consulta el calendario de eventos y manda a cada persona **un resumen** con sus alertas del día.
Cada alerta dice quién actúa y qué hacer, y trae adjunta la lista de pedidos en riesgo.

```bash
bi replay --from 2018-05-15 --to 2018-06-15     # la huelga de transportistas, día por día
bi run-daily --date 2018-05-27                  # el resumen que habría llegado ese domingo
bi impact                                       # qué costó cada evento
bi replay --deliver                             # todo, a la bandeja de demostración (outbox/)
```

## Lo que se mide, y cuándo se sabe

Un sistema en vivo no conoce el futuro, así que cada serie se fecha **el día en que su valor se
vuelve conocible**: la puntualidad de los pedidos *comprados* hoy no se sabe hasta dentro de
semanas.

| Serie | Fechada por | Por qué |
|---|---|---|
| Pedidos | compra | Se sabe en el momento |
| Ventas (R$) | semana de compra | Un solo pedido de R$ 10 mil mueve 4 σ un día regional |
| Entregas a tiempo | **fecha en que el pedido debía llegar** | Ese día se sabe si llegó. Olist solo promete días hábiles: la serie no tiene fines de semana |
| Reseñas negativas | semana de la reseña | Las del domingo son 28 % negativas y los lunes casi no hay |
| ⏱ Despachos a paquetería | entrega a la paquetería | **Indicador adelantado**: cae antes que la puntualidad |
| ⏱ Despachos atrasados | vencimiento del plazo de despacho | **Indicador adelantado**: de lo que debía salir ese día, lo que no salió |

Cada serie existe para Brasil y para cada región; una región con menos de 30 pedidos diarios se
evalúa por semana.

## El método

Puntuación z robusta: `(valor − mediana) / (1.4826 × MAD)` sobre las observaciones anteriores
(28 en series diarias, 20 días hábiles en puntualidad, 8 semanas en las semanales). Los conteos
se dividen antes por un factor de día de la semana. **|z| > 3.5 es anomalía; |z| ≥ 6 es
crítica.** Se vuelve **alerta** si va en la dirección mala de su KPI y es crítica (se envía ese
día) o dura dos periodos (se envía el segundo). Varios días seguidos son un episodio, una alerta.

No se juzgan: los meses incompletos, la cola del dataset (se desvanece desde el **23 de agosto
de 2018**: el primer día a partir del cual *todos* los siguientes quedan bajo la mitad de lo
normal) y los **festivos** (que nadie despache en Corpus Christi no es noticia).

### Lo que se corrigió midiendo

1. **La puntualidad daba cero anomalías**, ni con la huelga: la ventana era de "28 días de
   calendario" y la serie no tiene fines de semana. Ahora la ventana cuenta observaciones.
2. **Con 28 observaciones, la MAD es inestable**: sobre ruido puro el detector marca 6.9 días por
   cada 1,000, 15 veces lo que promete la normal. Una ventana de 8 semanas lo baja a 2.4 pero
   **deja de detectar la huelga**. Se conservó la ventana del diseño y el ruido lo controla la
   regla de persistencia: sobre 60 series de ruido de un año, **0.07 alertas enviadas al año**.
3. **Las fechas de envío**: una serie semanal se conoce al cerrar la semana (sale el domingo), y
   una alerta que persiste y luego se vuelve crítica sale cuando persistió.

## Calendario de eventos ([`config/events.yaml`](../config/events.yaml))

| Tipo | Ejemplo | Qué hace el sistema |
|---|---|---|
| **Planeado** | Carnaval, Black Friday, Navidad | No alerta por lo que el evento *debe* mover (el pico de pedidos del Black Friday es el plan funcionando), sí por lo demás (la puntualidad que cae en Navidad es un problema aunque la Navidad estuviera planeada). Sube la meta del mes (`plan_uplift`) y los meses siguientes la descuentan de su base |
| **No planeado** | Huelga de transportistas, crisis de marzo de 2018 | Se registra al confirmar una alerta. Sus días quedan fuera de la línea base *mientras dura*; las alertas de ese tramo y de sus 14 días posteriores llevan su nombre; `bi impact` mide su costo |

### Por qué "solo mientras dura": lo que se midió

El diseño decía excluir para siempre los días de un evento no planeado de la línea base, "para
no enseñarle al detector que lo anormal es normal". Medido sobre los datos reales, ninguna regla
simple gana en todo:

| Regla | Huelga: despachos | Huelga: puntualidad Brasil | Crisis de marzo |
|---|---|---|---|
| Solo los planeados fuera de la base | 25 may | 1 jun | 16 mar |
| Todos fuera para siempre (la del diseño) | 25 may | **no la detecta** | 9 mar |
| Todos fuera, más 14 días de secuelas | 24 may | 6 jun | 9 mar |
| **Los no planeados fuera solo mientras duran** (la elegida) | **25 may** | **1 jun** | **9 mar** |

Sacar para siempre los días de la huelga empuja la línea base de junio hasta abril, cuando la
puntualidad todavía se recuperaba de la crisis de marzo: la escala se infla y la caída de junio
no cruza el umbral. Sacarlos solo mientras dura el evento mantiene visible el evento largo (la
crisis de marzo se sigue juzgando contra la normalidad, no contra sí misma) sin cegar la secuela.

Un error que las pruebas de aceptación atraparon en el camino: al sacar días de la base, la
ventana seguía contando *posiciones*; durante un evento de un mes quedaba vacía y el detector
dejaba de evaluar justo los días que más importaban. Ahora toma las últimas N observaciones
*válidas*.

## Alerta temprana: la huelga se anticipó

La señal adelantada no fue la que el diseño esperaba. Los **despachos a paquetería** (~400 al
día) cayeron a 176 el 24 de mayo y a 131 el 25: las paqueterías dejaron de recoger. La alerta
salió el **25 de mayo**; la de puntualidad, el **1 de junio**: **7 días antes**.

```
25-may  ⏱ Despachos a paquetería cae en todo Brasil: 176 (esperado 345)
27-may  ⏱ Despachos a paquetería cae en Nordeste y Sul; Pedidos cae en Nordeste
30-may  ⏱ Despachos atrasados sube en Sudeste: 9.0 % (esperado 3.4 %)
01-jun  Entregas a tiempo cae en todo Brasil: 85.6 % (esperado 94.6 %), y en el Sudeste
```

**La crisis de marzo de 2018 no tuvo señal adelantada**: los vendedores despacharon normal y el
problema estuvo en el transporte. Se detecta, pero no se anticipa.

## Qué hacer: playbooks y pedidos en riesgo ([`config/playbooks.yaml`](../config/playbooks.yaml))

Cada serie y dirección tiene un responsable y pasos. Las de entregas y despachos adjuntan los
**pedidos en riesgo**: abiertos ese día en la región, que vencen en 7 días o ya vencieron (pedido,
estado y fechas; sin datos del cliente). En la historia, 33 alertas adjuntaron 26,282 pedidos.

Así llega al gerente del Nordeste el domingo 27 de mayo de 2018 (un correo, tres alertas):

```
Asunto: [BI] 🔴 3 alertas del dom 27-may-2018

🔴 ⏱ Despachos a paquetería cae en Nordeste: 50 (esperado 164)
Alerta temprana: este indicador se mueve antes que las entregas a tiempo.
Evento registrado en esas fechas: Huelga de transportistas.
Qué hacer — responsable: Logística
  1. Confirmar con las paqueterías si suspendieron recolecciones y desde cuándo.
  2. Ampliar en el sitio el plazo de entrega estimado para compras nuevas en la región.
  3. Atención a clientes avisa a los pedidos en riesgo (lista adjunta).
Pedidos en riesgo: 300 abiertos que vencen en 7 días o ya vencieron (lista adjunta).
...
Adjunto: pedidos_en_riesgo_carrier_pickups_Nordeste_….csv
```

**Una alerta por persona y día, no por anomalía.** La huelga levantó 11 alertas en 12 días; cada
persona las recibe agrupadas en un resumen diario, y Slack, en un solo mensaje por día.

## Qué costó cada evento (`bi impact`)

Contra lo que el detector esperaba sin el evento, en todo Brasil:

| Evento | Pedidos | Despachos | Entregas tarde de más | Calificación |
|---|---:|---:|---:|---|
| Black Friday 2017 (planeado) | **+142 %** | +97 % | 229 | 4.14 → 4.08 |
| Navidad 2017 (planeado) | −13 % | +2 % | 701 | 4.13 → **3.87** |
| Carnaval 2018 (planeado) | 0 % | +33 % | 52 | 3.97 → 4.01 |
| Crisis de marzo 2018 | −8 % | +3 % | **966** | 4.06 → **3.76** |
| Huelga de transportistas | **−50 %** | **−41 %** | 181 | 4.01 → 4.09 |

La huelga partió los pedidos a la mitad, pero apenas produjo entregas tardías y no bajó la
calificación: las fechas prometidas de Olist son tan holgadas que absorbieron el retraso. **La
crisis de marzo fue la que más dañó a los clientes.**

## Resultados (ene 2017 – ago 2018)

**57 alertas.** 26 caen en un evento registrado o en sus secuelas; las **31 restantes son 25
eventos en 120 meses-serie: 0.21 por serie al mes**, dentro del objetivo del diseño (≤ 1). El
Black Friday ya no alerta por sus pedidos (es planeado), pero sí por la puntualidad de las
semanas siguientes, que sí fue un problema.

[`tests/real`](../tests/real/test_real_events.py) convierte los criterios en pruebas que corren
donde están los datos reales (no en CI): el Black Friday se detecta (z > 20) pero no alerta, la
huelga se anticipa al menos 5 días, la crisis de marzo se detecta en su primera semana, el fin de
los datos no alerta y el ruido queda bajo una alerta por serie al mes.

## A quién le llega

| Alerta | Le llega a |
|---|---|
| De Brasil | `direccion`, `analista` |
| De una región | Su gerente regional; si es crítica, también `direccion` |
| De una región sin gerente | `direccion`, `analista`: ninguna alerta se queda sin destinatario |
| — | Los vendedores no reciben alertas: son por región, y una región incluye ventas de otros |

En **modo demo** cada correo se escribe como `.eml` (con sus CSV adjuntos) y cada mensaje de
Slack como `.json` en `outbox/`; nada sale de la máquina. En **modo live** se envían por SMTP y
por un webhook de Slack, con credenciales en variables de entorno.

En el dashboard, **Alertas → Anomalías** muestra cada serie con su rango normal, los eventos y las
alertas con responsable y pedidos en riesgo; **Alertas → Eventos**, el impacto de cada evento.

## Resiliencia del pipeline

| Riesgo | Medida | Prueba |
|---|---|---|
| La API de tipo de cambio falla | 3 intentos con espera creciente (1 s, 2 s), luego caché, luego tasas de respaldo | `test_fx_api_is_retried_before_falling_back` |
| Una carga falla a la mitad | Se construye en un archivo aparte y solo al final reemplaza al publicado: el dashboard sigue con la última carga buena | `test_a_failed_load_leaves_the_last_good_warehouse` (pasó de verdad en desarrollo) |
| Recargar con el dashboard abierto | El dashboard abre el almacén solo durante cada consulta y detecta datos nuevos | — |
| Datos incompletos | La cola del dataset y los meses incompletos no se juzgan | `test_end_of_the_data_is_incomplete_not_a_drop` |
| Tormenta de alertas | Un resumen por persona y día | `test_one_digest_per_person_per_day_with_the_lists_attached` |
| Datos corruptos | Calidad y cuarentena (paso 2) | `tests/data_quality` |

La prueba de volumen (Black Friday ×5) queda para el benchmark del paso 6.
