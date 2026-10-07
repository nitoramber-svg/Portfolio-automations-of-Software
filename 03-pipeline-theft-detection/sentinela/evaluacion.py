"""Evaluación del desempeño de detección (métricas al estilo API RP 1130 / API RP 1175).

- `evaluar_csv`: corre el sistema sobre un histórico real y compara sus incidentes
  contra las tomas confirmadas en campo (la "verdad" de referencia).
- `campana`: barrido Monte Carlo con el simulador para estimar sensibilidad
  (caudal mínimo detectable), error de ubicación, tiempo de detección y
  tasa de falsas alarmas antes de tener datos reales.
"""
import copy
import csv
import math
import random
from statistics import fmean, median

from .ejecucion import analizar_csv, ejecutar_escenario


def _p90(valores):
    s = sorted(valores)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)] if s else None


def comparar(incidentes, tomas, duracion_s, tolerancia_km=5.0, ventana_s=3600):
    """Empareja cada toma conocida con el primer incidente compatible en km y tiempo."""
    usados, detalle = set(), []
    for toma in sorted(tomas, key=lambda x: x["t_inicio"]):
        candidatos = [i for i in incidentes
                      if i["id"] not in usados
                      and abs(i["km"] - toma["km"]) <= tolerancia_km
                      and toma["t_inicio"] - 120 <= i["t_deteccion"] <= toma["t_inicio"] + ventana_s]
        if candidatos:
            inc = min(candidatos, key=lambda i: i["t_deteccion"])
            usados.add(inc["id"])
            detalle.append({**toma, "detectada": True, "incidente": inc["id"],
                            "error_km": abs(inc["km"] - toma["km"]), "tiempo_s": inc["t_deteccion"] - toma["t_inicio"]})
        else:
            detalle.append({**toma, "detectada": False})
    detectadas = [d for d in detalle if d["detectada"]]
    falsas = [i["id"] for i in incidentes if i["id"] not in usados]
    errores = [d["error_km"] for d in detectadas]
    tiempos = [d["tiempo_s"] for d in detectadas]
    return {
        "tomas": len(tomas),
        "detectadas": len(detectadas),
        "tasa_deteccion": len(detectadas) / len(tomas) if tomas else None,
        "falsas_alarmas": len(falsas),
        "falsas_por_dia": len(falsas) / (duracion_s / 86400) if duracion_s else None,
        "error_km_medio": fmean(errores) if errores else None,
        "error_km_p90": _p90(errores),
        "tiempo_s_mediana": median(tiempos) if tiempos else None,
        "tiempo_s_p90": _p90(tiempos),
        "detalle": detalle,
        "incidentes_sin_toma_conocida": falsas,
    }


def leer_tomas(ruta):
    """CSV con columnas t_inicio,km y opcionalmente caudal_m3h,descripcion (misma base de tiempo que el histórico)."""
    with open(ruta, encoding="utf-8") as f:
        return [{**fila, "t_inicio": float(fila["t_inicio"]), "km": float(fila["km"])} for fila in csv.DictReader(f)]


def evaluar_csv(cfg, ruta, ruta_tomas, ruta_bitacora=None, tolerancia_km=5.0):
    monitor, gestor = analizar_csv(cfg, ruta, ruta_bitacora)
    return comparar(list(gestor.incidentes.values()), leer_tomas(ruta_tomas), monitor.t + 1, tolerancia_km)


# ------------------------------------------------------------------ Monte Carlo
CAUDALES = (2, 3, 5, 8, 12, 18, 25, 40)


def _escenario(base, eventos, duracion, semilla):
    esc = copy.deepcopy(base)
    esc.update(eventos=sorted(eventos, key=lambda e: e["t"]), duracion_s=duracion, semilla=semilla)
    return esc


def campana(cfg, base, corridas=120, dias_sin_tomas=1, semilla=2026, salida=print):
    rng = random.Random(semilla)
    L = cfg["ducto"]["longitud_km"]
    resultados = []
    for n in range(corridas):
        toma = {"tipo": "toma", "t": 1200, "km": round(rng.uniform(5, L - 5), 1),
                "caudal_m3h": CAUDALES[n % len(CAUDALES)], "duracion_s": 3000,
                "apertura_s": 0 if (n // len(CAUDALES)) % 2 == 0 else rng.choice((300, 900, 1800))}
        _, gestor = ejecutar_escenario(cfg, _escenario(base, [toma], 4800, rng.randrange(10**6)))
        r = comparar(list(gestor.incidentes.values()), [{"t_inicio": 1200, "km": toma["km"]}], 4800)
        resultados.append({"caudal": toma["caudal_m3h"], "abrupta": toma["apertura_s"] == 0, "km": toma["km"],
                           "apertura_s": toma["apertura_s"], "incidentes": [(i["tipo"], i["km"], i["t_deteccion"]) for i in gestor.incidentes.values()],
                           "detectada": r["detectadas"] == 1, "falsas": r["falsas_alarmas"],
                           "error_km": r["detalle"][0].get("error_km"), "tiempo_s": r["detalle"][0].get("tiempo_s")})
        if (n + 1) % 20 == 0:
            salida(f"  {n + 1}/{corridas} corridas")

    # Operación normal sin tomas: maniobras aleatorias (la mitad sin registrar) y fallas de sensor.
    duracion = int(dias_sin_tomas * 86400)
    eventos, t = [], 1800
    while t < duracion - 1800:
        extremo = rng.choice(("entrada", "salida"))
        eventos.append({"tipo": "operacion", "t": t, "extremo": extremo, "delta_bar": rng.choice((-6, -3, 3, 6)),
                        "descripcion": f"Maniobra aleatoria en {extremo}", "registrada": rng.random() < 0.5})
        if rng.random() < 0.3:
            eventos.append({"tipo": "falla_sensor", "t": t + 600, "estacion": rng.choice([e["id"] for e in cfg["estaciones"]]),
                            "delta_bar": rng.choice((-2, -1, -0.5)), "duracion_s": 300})
        t += rng.randint(1800, 7200)
    _, gestor = ejecutar_escenario(cfg, _escenario(base, eventos, duracion, rng.randrange(10**6)))
    return resultados, {"dias": dias_sin_tomas, "maniobras": sum(e["tipo"] == "operacion" for e in eventos),
                        "falsas_alarmas": len(gestor.incidentes)}


def reporte_campana(resultados, normal):
    lineas = ["", "Sensibilidad por caudal de la toma (Monte Carlo con el simulador)",
              f"{'caudal m3/h':>11} | {'abrupta':>16} | {'apertura lenta':>16} | {'error km p90':>12} | {'tiempo p90':>10}"]
    for q in CAUDALES:
        fila = [r for r in resultados if r["caudal"] == q]
        celdas = []
        for abrupta in (True, False):
            grupo = [r for r in fila if r["abrupta"] == abrupta]
            det = sum(r["detectada"] for r in grupo)
            celdas.append(f"{det}/{len(grupo)} ({100 * det / len(grupo):.0f} %)" if grupo else "—")
        errores = [r["error_km"] for r in fila if r["detectada"]]
        tiempos = [r["tiempo_s"] for r in fila if r["detectada"]]
        lineas.append(f"{q:>11} | {celdas[0]:>16} | {celdas[1]:>16} | "
                      f"{(f'{_p90(errores):.2f}' if errores else '—'):>12} | "
                      f"{(f'{_p90(tiempos) / 60:.1f} min' if tiempos else '—'):>10}")
    total = len(resultados)
    lineas += ["",
               f"Detección global: {sum(r['detectada'] for r in resultados)}/{total}",
               f"Incidentes espurios durante las corridas con toma: {sum(r['falsas'] for r in resultados)}",
               f"Operación normal: {normal['dias']} día(s), {normal['maniobras']} maniobras (≈50 % sin registrar) "
               f"→ {normal['falsas_alarmas']} falsas alarmas"]
    return "\n".join(lineas)
