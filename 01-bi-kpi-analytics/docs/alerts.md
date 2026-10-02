# Anomalías y alertas

> Paso 5 de 6. Todo medido sobre el dataset real; `bi replay` reproduce cada alerta.

## Qué hace

Cada noche (`bi run-daily`) el sistema compara cada KPI de cada región con su propio pasado y
avisa, por correo y Slack, solo a quien le corresponde. `bi replay --from 2017-01-01` recorre la
historia día por día y muestra qué alertas habrían llegado, cuándo y a quién.

```bash
bi replay --from 2018-05-15 --to 2018-06-15     # la huelga de transportistas
bi run-daily --date 2017-11-24                  # el Black Friday, como habría llegado
bi replay --deliver                             # todas, a la bandeja de demostración (outbox/)
```

## Lo que se mide, y cuándo se sabe

Un sistema en vivo no conoce el futuro, así que cada serie se fecha **el día en que su valor se
vuelve conocible**. Esto importa más de lo que parece: la puntualidad de los pedidos *comprados*
hoy no se sabe hasta dentro de semanas.

| Serie | Fechada por | Por qué |
|---|---|---|
| Pedidos | compra | Se sabe en el momento |
| Ventas (R$) | semana de compra | Un solo pedido de R$ 10 mil mueve 4 σ un día regional |
| Entregas a tiempo | **fecha en que el pedido debía llegar** | Ese día se sabe si llegó. Olist solo promete días hábiles: la serie no tiene fines de semana ni festivos |
| Reseñas negativas | semana de la reseña | Las del domingo son 28 % negativas y los lunes casi no hay: los días no son comparables |

Cada serie existe para Brasil y para cada región. Una región con menos de 30 pedidos diarios
(Norte, y casi todas en ventas) se evalúa por semana.

## El método

Puntuación z robusta (diseño §6): `(valor − mediana) / (1.4826 × MAD)` sobre las observaciones
anteriores (28 en series diarias, 20 días hábiles en puntualidad, 8 semanas en las semanales).
Las series de conteo se dividen antes por un factor de día de la semana (mediana del mismo día
÷ mediana total en las 8 semanas previas). **|z| > 3.5 es anomalía; |z| ≥ 6 es crítica.**

Una anomalía se vuelve **alerta** si:
- va en la dirección mala de su KPI (la puntualidad que *sube* no es alerta; los pedidos, en
  ambas: un pico es noticia), y
- es crítica (se envía ese mismo día) o dura dos periodos (se envía el segundo).

Varios días seguidos de lo mismo son **un episodio, una alerta**, con su pico y su duración.

**No se juzgan los datos incompletos:** los meses incompletos del paso 2 y la cola del dataset.
Olist termina desvaneciéndose a fines de agosto de 2018; el corte se detecta como el primer día
a partir del cual *todos* los siguientes quedan bajo la mitad de lo normal (el 23 de agosto de
2018). Desde ahí, nada se marca como caída.

### Tres cosas que se corrigieron midiendo

1. **La puntualidad daba cero anomalías**, ni siquiera con la huelga. La ventana era de "28 días
   de calendario" y la serie no tiene fines de semana: nunca juntaba datos suficientes. Ahora la
   ventana cuenta observaciones.
2. **Con 28 observaciones, la MAD es una escala inestable.** Sobre ruido puro, el detector marca
   6.9 días por cada 1,000, 15 veces lo que promete la normal (0.47). Una ventana de 8 semanas lo
   baja a 2.4, pero entonces **la huelga deja de detectarse**: la crisis de marzo infla la línea
   base. Se conservó la ventana del diseño y el ruido se controla con la regla de persistencia.
   Medido sobre 60 series de ruido de un año: 1.9 días anómalos al año, **0.07 alertas enviadas**.
3. **La fecha de envío.** Una serie semanal solo se conoce al cerrar la semana: su alerta sale
   el domingo, no el lunes en que empieza. Y una alerta que persiste y luego se vuelve crítica
   sale cuando persistió, no cuando se volvió crítica.

## Resultados con los datos reales (ene 2017 – ago 2018)

**50 alertas**, que agrupadas por KPI y semana son **33 eventos**:

| Evento | Qué detectó | Alertas |
|---|---|---:|
| **Black Friday** (24-nov-2017) | Pedidos +26 σ en Brasil; ventas y pedidos también en 4 regiones | 10 |
| **Temporada navideña** (dic-2017) | Entregas a tiempo cae en Brasil, Nordeste y Centro-Oeste; suben las reseñas negativas | 5 |
| **Crisis de entregas de marzo de 2018** | La puntualidad de lo que debía llegar cae de 92 % a 70 % a partir del 7 de marzo; tarda hasta abril en recuperarse. Las reseñas negativas suben 12 σ | 7 |
| **Huelga de transportistas** (21–31 may-2018) | Los pedidos que debían llegar justo después: −8 σ en Brasil, −11 σ en el Sudeste. Caen los pedidos en el Sul | 5 |
| Sin causa identificada | Picos regionales de ventas, semanas flojas de puntualidad en ago-2018 | 23 |

Las 23 sin causa identificada son **19 eventos en 80 meses-KPI: 0.24 por KPI al mes**, dentro
del objetivo del diseño (≤ 1). El peor mes es la puntualidad de agosto de 2018 (3 semanas
seguidas en distintas regiones). Lo que no se sabe es si son ruido o eventos reales que Olist no
documentó; la causa de la crisis de marzo de 2018 tampoco está confirmada.

[`tests/real`](../tests/real/test_real_events.py) convierte estos criterios en pruebas que corren
donde están los datos reales (no en CI).

## A quién le llega

El enrutamiento sigue la seguridad por rol ([`config/alerts.yaml`](../config/alerts.yaml)):

| Alerta | Le llega a |
|---|---|
| De Brasil | `direccion`, `analista` |
| De una región | Su gerente regional; si es crítica, también `direccion` |
| De una región sin gerente | `direccion`, `analista`: ninguna alerta se queda sin destinatario |
| — | Los vendedores no reciben alertas: son por región, y una región incluye ventas de otros |

En **modo demo** (el predeterminado) cada correo se escribe como `.eml` y cada mensaje de Slack
como `.json` en `outbox/`; nada sale de la máquina. En **modo live** se envían por SMTP y por un
webhook de Slack, con credenciales en variables de entorno, nunca en el repositorio.

En el dashboard, la página **Alertas → Anomalías** muestra las alertas que el usuario puede ver y
cada serie con su rango normal y sus puntos anómalos.

## Pendiente (paso 5b)

Calendario de eventos (el Black Friday como evento *planeado*: ajusta la meta y no se alerta),
indicadores adelantados, playbooks por alerta con la lista de pedidos en riesgo, análisis de
impacto y pruebas de resiliencia del pipeline.
