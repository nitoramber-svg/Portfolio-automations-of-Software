# Guía del tablero del taller

Un tablero web para un taller de muebles a la medida. Cada área carga sus listas de Excel y el
tablero dice cuántas cotizaciones se ganan, cuánto tarda cada etapa del taller, cuánto se gana
en cada pieza, qué falta por facturar y **qué pedidos van a llegar tarde, y por qué**.

No hay que instalar nada: se abre desde el navegador, en computadora, tableta o celular.

## Lo que hay que cargar

Cuatro plantillas de Excel y las facturas. Se descargan en **Cargar datos**. Cada plantilla trae
una hoja de instrucciones, y las columnas con opciones (estado, etapa, concepto) traen una lista
para elegir. Se puede pegar lo que exporte el sistema que ya usan, siempre que los encabezados de
la primera fila se queden igual.

Va **una fila por pieza**: una cotización con tres piezas son tres filas con el mismo folio.

| Plantilla | Qué lleva | Quién la llena | Cada cuándo |
|---|---|---|---|
| Cotizaciones | Cada pieza cotizada: cliente, vendedor, marca, importe, si se ganó o se perdió y por qué | Ventas | Cada semana |
| Pedidos | Cada pieza vendida: precio, **fecha prometida** y fecha de entrega | Ventas | Cada semana |
| Producción | Cada etapa del taller por la que pasa cada pieza, con su fecha de inicio y de fin | Taller | Cada semana |
| Costos | Lo que costó cada pedido: materiales, tela, mano de obra, flete | Administración | Cada mes |
| Facturas | Los XML del SAT, sueltos o en el ZIP de la descarga masiva. No llevan plantilla | Administración | Cada mes |

Volver a cargar una plantilla **reemplaza** la anterior; las demás se quedan igual. Si el taller
solo actualiza producción, sube solo esa.

**Si algo está mal escrito**, el tablero no adivina. Dice el archivo, la fila de Excel y qué
corregir ("fila 14: la fecha de entrega es anterior al pedido"). Esa fila no se carga y el resto
sí.

## Qué ve cada área

| Área | Ve | No ve |
|---|---|---|
| Dirección | Todo | — |
| Ventas | Cotizaciones, pedidos, entregas y pedidos en riesgo | Costos, margen ni facturas |
| Vendedor | Lo mismo que ventas, solo de sus clientes | Lo de los demás vendedores |
| Taller | Producción y pedidos por entregar | Precios, costos ni facturas |
| Administración | Pedidos, margen y facturación | Cotizaciones |

## Cómo leer cada página

**Resumen.** Los cinco números del periodo y una lectura en palabras: qué se vendió, cuántas
cotizaciones se ganaron, qué etapa es la más lenta, qué margen bajó y por qué.

**Cotizaciones.** La **tasa de cierre** es cuántas se ganaron de las que ya se decidieron (las
abiertas no cuentan). Hay dos:
- **Por número**: de cada 10 cotizaciones, cuántas se ganan.
- **Por monto**: de cada peso cotizado, cuánto se gana.

Si la tasa por monto es menor, las cotizaciones grandes se pierden más. Al final está la lista de
cotizaciones sin respuesta desde hace más de 45 días: vale la pena llamarles.

**Pedidos y entregas.** Un pedido llegó **a tiempo** si su última pieza se entregó en o antes de
la fecha prometida.

**Taller.** Cuánto tarda cada etapa:
- **Típico**: la mitad de las piezas tarda menos que eso.
- **Casos lentos**: 8 de cada 10 tardan menos que eso.

También muestra cuánto trabajo hay hoy en cada etapa y qué piezas llevan más tiempo en la suya.

**Pedidos en riesgo.** Cada pieza sin entregar, con su estado:

| Estado | Qué significa |
|---|---|
| 🔴 Vencido | Ya pasó la fecha prometida |
| 🟠 Va tarde | Al ritmo actual del taller, se termina después de la fecha prometida |
| 🟡 Justo | Llega, pero con menos de 5 días de margen |
| 🟢 En tiempo | Llega con holgura |

La columna **Por qué** dice en qué etapa está, si ya va más lenta de lo normal y qué le falta. La
fecha estimada suma lo que han tardado normalmente las etapas que le faltan en los últimos 120
días. La página muestra cuánto acertó ese cálculo con el historial.

**Margen.** Precio de venta menos costos, de los pedidos **ya entregados**: hasta la entrega no
están todos los costos, como el flete. Las piezas sin costos cargados se cuentan aparte, porque con
costo cero parecería que se ganó todo.

**Facturación.** Lo facturado ante el SAT, los pedidos entregados que no tienen factura, las
facturas que ningún pedido menciona y los montos que no cuadran. Para ligar un pedido con su
factura, la plantilla de Pedidos lleva la columna **Folio de factura**: sirve la serie y folio
(A-1234) o el UUID.

## Preguntas frecuentes

**¿Y si no tenemos la fecha prometida, o los costos?** El tablero funciona con lo que haya. Sin
fecha prometida no puede medir retrasos; sin costos, no hay margen. Lo dice en la página que
corresponda.

**¿Qué tan bien tiene que estar llenado?** Las columnas obligatorias son pocas y van en amarillo
en las instrucciones. Acepta fechas como 15/03/2026, importes como $12,500.00 y no distingue
mayúsculas ni acentos.

**¿Dónde quedan nuestros datos?** En la demostración, lo que se carga solo lo ve quien lo cargó
y se borra al cerrar la página. Para usarlo con datos reales hay que decidir dónde se guardan y
dar a cada persona su usuario; eso se decide con la empresa antes de cargar nada real.

**¿Por qué "Datos al" no es la fecha de hoy?** Es el último día con algo registrado. Si las
listas se suben cada lunes, el viernes el tablero sigue midiendo contra el lunes, y no marca
como vencido algo que quizá ya se entregó.

**Un link para cada área.** Agregar `?ver=taller` (o `ventas`, `administracion`, `direccion`)
al final del link abre el tablero directo en esa vista.
