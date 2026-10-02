# Guía de uso del tablero

> Para quien usa el tablero, no para quien lo programa. Cómo abrirlo, qué dice cada página y
> qué hacer cuando llega una alerta.

## Abrirlo

```bash
bi dashboard
```

Se abre solo en el navegador, en http://localhost:8501. Si el navegador se cierra, el tablero
sigue funcionando: basta volver a esa dirección. Si se cierra la terminal o se apaga la
computadora, hay que volver a correr `bi dashboard` (los datos no se pierden; no hace falta
volver a cargarlos).

## La barra lateral

| Control | Qué hace |
|---|---|
| **Usuario** | Quién eres. Cada rol ve solo lo que le corresponde (abajo). Un enlace con `?usuario=gerente.nordeste` abre el tablero ya como ese usuario |
| **Fechas de compra** | Por defecto, los meses completos (ene 2017 – ago 2018). Las ventas, las entregas y las reseñas se fechan por la compra, así que un filtro significa lo mismo en todas las páginas |
| **Región, estado, categoría** | Filtran todas las páginas. Un gerente solo puede elegir sus regiones |
| **Moneda** | Reales, pesos o dólares, al tipo de cambio del día de cada compra |

### Qué ve cada rol

| Rol | Ve |
|---|---|
| Dirección | Todo |
| Analista | Todo, pero sin datos individuales de clientes |
| Gerente regional | Solo los pedidos de clientes de su región |
| Vendedor | Solo sus propias ventas; no las de otros vendedores en el mismo pedido, ni el total de esos pedidos |

Si un número parece no cuadrar entre dos personas, lo primero es revisar con qué usuario está
cada una: el mismo filtro da números distintos según el rol, a propósito.

## Las páginas

**Resumen ejecutivo** — Las cinco cifras que importan contra su meta (en verde si se cumple, en
rojo si no), las ventas de cada mes contra la meta, las regiones, los KPIs fuera de meta y un
párrafo de **Lectura** que dice en palabras qué pasó.

![Resumen](screenshots/01-resumen.png)

**Ventas** — La tendencia, un mapa por estado (tamaño: ventas; color: puntualidad), las
categorías, los mejores vendedores, las formas de pago y las mensualidades.

**Operación y satisfacción** — Entregas a tiempo, días de entrega, flete, cancelaciones, la
calificación y una gráfica clave: **retraso contra calificación**. Un pedido a tiempo promedia
4.29 estrellas; un solo día tarde, 3.73; una semana o más, 1.73.

**Alertas** — Tres pestañas:

- **Anomalías**: lo que el sistema detectó comparando cada indicador con su propio pasado. Elige
  una serie y una región para ver su rango normal (banda azul), los puntos fuera de él (rojos) y
  los eventos (bandas azules: planeados; naranjas: no planeados). Debajo, cada alerta enviada,
  con su responsable y cuántos pedidos puso en riesgo.
- **Eventos**: el Black Friday, la Navidad, la huelga de 2018… y lo que costó cada uno.
- **Contra la meta**: cada KPI con meta, mes a mes (rojo: fuera de meta; gris: menos de 30
  pedidos, demasiado poco para juzgar).

![Alertas](screenshots/04-alertas.png)

**Calidad de datos** (dirección y analistas) — Las 30 pruebas que corren en cada carga, los
pedidos apartados en cuarentena con su motivo, y la conciliación que confirma que no se perdió ni
se duplicó nada.

## Cuando llega una alerta

Llega **un correo por día** con todas tus alertas (y un mensaje diario al canal de Slack).
Cada alerta dice:

1. **Qué pasó y contra qué se comparó**: "Entregas a tiempo cae en Sudeste: 86.3 % (esperado
   95.6 %)".
2. **Si es alerta temprana** (⏱): esos indicadores se mueven antes que las entregas. En la
   huelga de 2018 avisaron 7 días antes.
3. **Si cae en un evento registrado** (por ejemplo, la huelga).
4. **Qué hacer y quién**: el responsable y los pasos.
5. **Pedidos en riesgo**, en un CSV adjunto: los abiertos de tu región que vencen en 7 días o ya
   vencieron, para avisarles antes de que se quejen.

Severidad: 🔴 crítica (llega el mismo día); 🟠 advertencia (llega cuando se repite dos veces
seguidas, para no avisar por un día raro).

## Preguntas frecuentes

**¿Por qué el cumplimiento de meta sale con "—"?** La meta existe por región y mes. Para un
vendedor, una categoría o un estado no hay meta: queda vacía en lugar de inventarse.

**¿Por qué septiembre y octubre de 2018 no aparecen en las tendencias?** El dataset termina a
medias: desde el 23 de agosto de 2018 los datos se desvanecen. Esos meses se marcan como
incompletos y no se comparan ni disparan alertas.

**¿Por qué el Black Friday no generó una alerta de "pedidos suben"?** Es un evento planeado: el
pico es el plan funcionando. Sí alertó por la puntualidad de las semanas siguientes, que sí fue
un problema.

**¿Cómo se agrega un KPI, una meta o un evento?** Son archivos de configuración, sin programar:
`config/kpis.yaml`, `config/targets.yaml`, `config/events.yaml`. Después, `bi load`.
