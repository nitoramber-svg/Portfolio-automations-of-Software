# Sentinela Ductos — análisis del problema y de la solución

*Resumen en inglés, capturas y resultados: [README](../README.md). Despliegue y operación 24/7: [despliegue/README.md](../despliegue/README.md).*

**Sistema de detección, localización y respuesta a tomas clandestinas en ductos de Pemex.**

---

## 1. La empresa y el problema

**Empresa:** Petróleos Mexicanos (Pemex), la petrolera del Estado mexicano.

**Problema:** el robo de combustible en ductos (*huachicol*) mediante tomas clandestinas.

| Indicador | Cifra |
|---|---|
| Pérdidas por robo de combustible en 2025 | **$23,491 millones de pesos**, 14.4 % más que en 2024 |
| Pérdida promedio diaria en 2025 | **$64.3 millones de pesos** |
| Tomas clandestinas detectadas en 2025 | **9,366** |
| Ritmo en el 1T-2026 | 2,563 tomas, **una cada 51 minutos** |
| Pérdidas en el 1S-2026 | $9,180 millones de pesos |

Además del dinero, cada toma pone vidas en riesgo: hay explosiones, incendios y contaminación de suelo y mantos acuíferos. También financia al crimen organizado, y la producción de Pemex se ve afectada cuando hay que cerrar un ducto.

### Por qué la respuesta actual no basta

1. **Detección tardía.** El "diablo instrumentado", que recorre el ducto por dentro, encuentra las perforaciones, pero el reporte tarda **30 días o más**. En ese tiempo la toma ya se ordeñó y se abandonó.
2. **Monitoreo SCADA incompleto.** La Auditoría Superior señaló fallas de mantenimiento y monitoreo del SCADA, y sitios de medición que nunca se terminaron de instalar. Algunos ductos no tienen sensores.
3. **Falsas alarmas.** Los paros de bomba, las aperturas de válvulas y las fallas de transmisores también producen caídas de presión. Si el sistema no las distingue, los operadores dejan de creer en las alarmas.
4. **La alarma no se convierte en acción.** Saber que "algo pasa entre el km 40 y el km 60" no basta para mandar a una brigada. Hace falta un kilómetro preciso, el acceso más cercano, una brigada asignada y un registro auditable.
5. **Las tomas lentas.** Los grupos más sofisticados abren la válvula poco a poco para no provocar un golpe de presión. Estas tomas solo aparecen en el balance volumétrico.

## 2. La solución: Sentinela Ductos

Es un software que se conecta a las lecturas que **un SCADA ya entrega**: presión en las estaciones y caudal en las terminales. Con esas lecturas, en segundos o minutos:

1. **detecta** la toma,
2. **la localiza** con un margen de cientos de metros,
3. **descarta** maniobras operativas y fallas de sensor, y
4. **convierte la alarma en un incidente con acciones**: brigada sugerida, ETA, acceso carretero más cercano, litros perdidos en tiempo real, seguimiento hasta el cierre y bitácora auditable.

### Arquitectura

```
 SCADA (HTTP / OPC-UA / CSV)              Bitácora operativa (maniobras)
        │ presión por estación, caudal entrada/salida              │
        ▼                                                         ▼
 INGESTA (ingesta.py): valida, reordena, rellena huecos → 1 muestra/s
        │                                                         │
        ▼                                                         ▼
 ┌──────────────────────────── MONITOR (monitor.py) ───────────────────────────┐
 │  1. Onda de presión negativa ──► triangulación del km  (detección < 3 min)  │
 │  2. Balance volumétrico + CUSUM ──► confirma y mide el caudal robado        │
 │  3. Perfil de presión ──► localiza tomas lentas sin onda                    │
 │  Fusión: ¿toma? ¿maniobra registrada? ¿transitorio de terminal? ¿sensor?    │
 └──────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
        GESTOR DE INCIDENTES (incidentes.py) ── SQLite (registro auditable)
        brigada más cercana, ETA, acceso, litros y $ perdidos, estados
                                    ▼
        API HTTP + TABLERO WEB (servidor.py, web/index.html)
```

### Los tres algoritmos

**1. Onda de presión negativa (NPW).** Cuando se perfora un ducto presurizado, la presión cae de golpe en ese punto. El frente de caída viaja en ambos sentidos a la velocidad del sonido en la gasolina, unos 1.1 km/s. Para cada estación, el sistema:
- detecta el escalón comparando una línea base de 35 s contra los últimos 3 s,
- calcula la hora de llegada al 50 % de la caída, interpolada para tener resolución menor a un segundo,
- ubica la toma resolviendo `t_k = t0 + |x_k − x| / v` por mínimos cuadrados ponderados. El peso de cada estación es la amplitud al cuadrado, porque las estaciones lejanas tienen una señal más débil y una hora menos precisa.

**2. Balance volumétrico con CUSUM.** El faltante es lo que entra menos lo que sale. Al arrancar, el sistema aprende el sesgo de los medidores. Después acumula evidencia con CUSUM (`S = max(0, S + d − k)`), lo que permite detectar faltantes pequeños y sostenidos sin disparar alarmas por el ruido. Durante 10 minutos después de una maniobra registrada no evalúa, porque el empaque y desempaque de la línea genera desbalances temporales. Este método también mide el caudal robado y acumula los litros perdidos.

**3. Perfil de presión.** Una extracción sostenida en el km *x* deforma el perfil estacionario en forma de triángulo con vértice en *x*. Ajustar ese triángulo a las caídas medidas en cada estación localiza las tomas de apertura lenta, que no generan onda.

**Fusión y descarte de falsas alarmas:**

| Lo que se observa | Clasificación | Acción |
|---|---|---|
| Frente en 1 sola estación | Falla de transmisor | Orden de mantenimiento, sin brigada |
| Frente con origen en una terminal o que coincide con la bitácora | Maniobra operativa | Se registra; si no estaba en la bitácora, se avisa a control |
| Frente con origen a mitad del ducto | Toma probable | Incidente de confianza media |
| … y después aparece un faltante volumétrico | Toma confirmada | Confianza alta y caudal medido |
| Faltante volumétrico sin frente previo | Extracción gradual | Incidente localizado con el perfil de presión |

## 3. Resultados con el escenario de demostración

El escenario simula 3 horas de operación en un ducto de 120 km con 7 estaciones:

| Evento simulado | Resultado del sistema |
|---|---|
| Paro y arranque de bomba registrados | Descartado como maniobra ✔ |
| Falla de transmisor en E3 | Orden de mantenimiento, sin brigada ✔ |
| Toma abrupta de 18 m³/h en el **km 47.3** | Detectada en **2.3 min**, ubicada en el **km 47.15** (150 m de error), confirmada en 3.7 min con 17.1 m³/h; volumen estimado de 11,625 L contra 12,000 L reales |
| Apertura de válvula en la terminal, sin registrar en bitácora | Descartada; se pide avisar a control ✔ |
| Toma lenta de 10 m³/h en el **km 93.8** | Detectada por balance en el **km 93.7**; volumen estimado de 6,960 L contra 7,080 L reales |
| **Falsas alarmas** | **0** |

## 4. Cómo usarlo

Necesita Python 3.10 o más reciente. **No tiene dependencias externas.**

```bash
cd 03-pipeline-theft-detection
python -m sentinela demo                 # abre el tablero en vivo en http://127.0.0.1:8765
python -m sentinela demo --velocidad 120 # simulación más rápida
python -m sentinela simular --salida lecturas.csv
python -m sentinela analizar lecturas.csv
python -m unittest -v                    # 37 pruebas
python -m sentinela campana              # Monte Carlo: sensibilidad y falsas alarmas
python -m sentinela evaluar historico.csv --tomas tomas_confirmadas.csv
```

En el tablero, el operador puede **despachar brigada → marcar en sitio → cerrar como toma sellada o falsa alarma**. Cada paso queda guardado en `registros/turno_*.db`.

**Para usarlo con un ducto real** se edita `sentinela/datos/ducto_demo.json`, que contiene la longitud, la velocidad de onda, las estaciones, los accesos, las brigadas y los umbrales.

### Modo tiempo real

```bash
# Servidor que recibe lecturas reales
python -m sentinela servidor --token MI_TOKEN
# Con suscripción OPC-UA al SCADA (requiere: pip install asyncua)
python -m sentinela servidor --token MI_TOKEN --opcua sentinela/datos/mapa_opcua_ejemplo.json
# Prueba: emisor que manda lecturas simuladas por la red con 2 % de pérdida, desorden y datos basura
python -m sentinela servidor --token MI_TOKEN --sin-verificar-reloj
python -m sentinela emisor --token MI_TOKEN --velocidad 25 --perdida 0.02 --desorden --basura
```

**Formato de entrada** (`POST /api/lecturas`, encabezado `Authorization: Bearer <token>`):

```json
{"lecturas": [{"etiqueta": "E2", "ts": 1791393600.25, "valor": 58.07},
              {"etiqueta": "q_entrada", "ts": 1791393600.40, "valor": 901.3}]}
```

También acepta una foto completa de un instante: `{"ts": ..., "presiones": {"E0": ..., ...}, "q_entrada": ..., "q_salida": ...}`. Las maniobras se registran con `POST /api/bitacora` y el cuerpo `{"ts": ..., "descripcion": "..."}`.

**Cómo maneja los datos imperfectos** ([ingesta.py](sentinela/ingesta.py)):

| Situación real | Qué hace el sistema |
|---|---|
| Etiqueta desconocida, valor no numérico, fuera de rango físico, hora futura | Rechaza la lectura y cuenta el motivo |
| Lecturas desordenadas o retrasadas | Espera `--latencia` s (3 por defecto) y las reordena por hora de origen |
| Lectura que llega después de procesar su segundo | Se rechaza como "tardía" |
| Estación sin datos ≤ `--max-hueco` s | Repite su último valor |
| Estación sin datos por más tiempo | Suspende la detección, avisa en la bitácora y, al volver los datos, reinicia los detectores para no confundir el salto con una toma |
| SCADA sin enviar nada | Marca "Enlace caído" en el tablero |

### Prueba con OPC-UA

```bash
pip install asyncua
python herramientas/servidor_opcua_prueba.py --velocidad 20      # SCADA de prueba
python -m sentinela servidor --sin-verificar-reloj --opcua sentinela/datos/mapa_opcua_ejemplo.json
```

## 4b. Validación del desempeño

**Campaña Monte Carlo** (`python -m sentinela campana`): 160 tomas simuladas con caudal, km y forma de apertura aleatorios, más 2 días de operación normal con 34 maniobras, la mitad sin registrar en bitácora.

| Caudal de la toma | Abrupta | Apertura lenta (5–30 min) | Error de ubicación p90 |
|---|---|---|---|
| 2–3 m³/h | 0 % | 0 % | — |
| 5 m³/h | 100 % | 100 % | 1.8 km |
| 8 m³/h | 100 % | 100 % | 2.2 km |
| 12–40 m³/h | 100 % | 100 % | 0.25–0.65 km |

- **Falsas alarmas: 0** en 2 días de operación normal.
- **Incidentes espurios o duplicados: 0.**
- Las tomas abruptas se detectan en ~2.3 min. Las de apertura lenta tardan entre 15 y 50 min, según su caudal.
- **El umbral de sensibilidad está entre 3 y 5 m³/h** (≈0.5 % del caudal nominal).

Esta campaña sacó a la luz 4 defectos que ya se corrigieron y tienen pruebas de regresión: arranques de bomba no registrados que se tomaban como extracción, rampas de presión que se confundían con ondas, ubicación ambigua en los tramos extremos y duplicados de tomas pequeñas.

**Con datos reales** (`python -m sentinela evaluar`): se corre el sistema sobre un histórico del SCADA y se compara contra un CSV de tomas confirmadas en campo (`t_inicio,km[,caudal_m3h,descripcion]`). El reporte da la tasa de detección, las falsas alarmas por día, el error de ubicación y el tiempo de detección, que son las métricas que pide la evaluación de sistemas de detección de fugas según API RP 1130 / 1175. Los "incidentes sin toma conocida" deben revisarse en campo: pueden ser falsas alarmas o tomas que nadie había encontrado.

## 4c. Seguridad y operación 24/7

| Necesidad | Qué hace el sistema |
|---|---|
| Que no cualquiera despache o cierre | Usuarios con roles (lectura < operador < supervisor < admin), contraseñas PBKDF2, bloqueo tras 5 intentos fallidos y cada acción firmada con el usuario |
| Cifrado | HTTPS con `--certificado/--llave`, y OPC-UA con Basic256Sha256 + SignAndEncrypt; un cliente sin cifrado es rechazado |
| No exponerse por error | El servidor se niega a escuchar en la red sin usuarios, HTTPS y token de máquina |
| Respaldos | Copia en caliente de la base cada hora, con rotación |
| Reinicios y cortes de luz | Al arrancar con la misma base se recuperan incidentes, bitácora y base de tiempo |
| Guardia 24/7 | `sentinela vigia` avisa por Telegram o webhook si el sistema cae o aparece un incidente |
| IEC 62443 | Solo lectura sobre el SCADA, zona DMZ y servicio systemd endurecido (ver la guía de despliegue) |

## 5. Ruta a producción

| Fase | Entregable |
|---|---|
| Piloto (3 meses) | Conector OPC-UA/Modbus al SCADA de **un** ducto crítico (p. ej. un tramo de Hidalgo, el estado con más tomas). Calibrar umbrales con datos históricos y tomas ya documentadas. |
| Escalamiento | Varios ductos, base de datos PostgreSQL/TimescaleDB, autenticación por rol (control, seguridad física, mantenimiento) y app móvil para las brigadas con navegación al acceso. |
| Integración | Coordenadas GIS reales del trazado, envío automático a Guardia Nacional y Sedena, y cruce con los inventarios de las terminales para detectar *huachicol fiscal*. |
| Sensores adicionales | En tramos sin SCADA: transmisores de presión de bajo costo con enlace celular o satelital. En tramos críticos: fibra óptica acústica (DAS) para detectar la excavación **antes** de que perforen. |

**Impacto potencial:** reducir las pérdidas apenas **10 %** equivale a unos **$2,300 millones de pesos al año**, contra un costo de software e integración que es una fracción mínima de esa cifra.

### Limitaciones honestas

- El simulador usa un modelo hidráulico **simplificado**: resistencia linealizada y onda con atenuación exponencial. Sirve para demostrar y probar la lógica, no para certificarla. Los umbrales deben calibrarse con datos reales de cada ducto.
- La precisión depende de cada cuántos km haya estaciones y de que sus relojes estén sincronizados. Un error de 1 s equivale a unos 550 m de error en la ubicación, así que en producción hace falta sincronizar con GPS/NTP.
- Las tomas muy pequeñas (menos de 3 m³/h) quedan por debajo de la sensibilidad del balance. Para esos casos se necesitan las tecnologías complementarias de la fase de sensores adicionales.

## Fuentes

- [Pemex pierde más de 64 mdp al día por toma clandestina — El CEO](https://elceo.com/negocios/pemex-pierde-mas-de-64-mdp-al-dia-por-toma-clandestina-de-ductos-y-huachicol-fiscal/)
- [Pemex eleva pérdidas por robo de combustible — Expansión (ago 2026)](https://expansion.mx/empresas/2026/08/10/pemex-eleva-perdidas-por-robo-de-combustible-en-tres-meses)
- [Huachicol le pega a las finanzas de Pemex, 2T-2026 — Infobae](https://www.infobae.com/mexico/2026/08/05/huachicol-le-pega-a-la-finanzas-de-pemex-estas-son-las-millonarias-perdidas-durante-el-segundo-trimestre-de-2026/)
- [Sube 15.3 % huachicol según informe de Pemex a EU — Zeta Tijuana](https://zetatijuana.com/2026/05/pemex-reporta-mas-huachicol-robo-de-combustible-crece-15-3/)
- [Estrategia contra el huachicol insuficiente — El CEO](https://elceo.com/negocios/huachicol-mexico-pemex-estrategia-insuficiente/)
- [Las armas de Pemex contra el huachicoleo (diablo instrumentado) — El CEO](https://elceo.com/politica/las-armas-de-pemex-contra-el-huachicoleo/)
- [La Auditoría acusa a Pemex de negligencia en sistema contra huachicoleo — Expansión](https://expansion.mx/empresas/2019/02/21/la-auditoria-acusa-pemex-de-negligencia-sistema-huachicoleo)
