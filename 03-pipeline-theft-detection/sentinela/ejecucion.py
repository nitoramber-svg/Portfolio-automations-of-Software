"""Fuentes de datos: simulación en vivo y reproducción de archivos CSV del SCADA."""
import csv
import threading
import time

from .almacen import Almacen
from .incidentes import GestorIncidentes
from .monitor import Monitor
from .simulador import Simulador


def crear_sistema(cfg, ruta_bd=":memory:"):
    gestor = GestorIncidentes(cfg, Almacen(ruta_bd))
    return Monitor(cfg, gestor), gestor


def alimentar(monitor, lectura):
    for op in lectura.operaciones:
        monitor.registrar_operacion(lectura.t, op["descripcion"])
    monitor.procesar(lectura.t, lectura.presiones, lectura.q_entrada, lectura.q_salida)


def ejecutar_escenario(cfg, escenario, duracion=None):
    """Corre el escenario completo sin pausas (pruebas y análisis por lotes)."""
    monitor, gestor = crear_sistema(cfg)
    sim = Simulador(cfg, escenario)
    for t in range(duracion or sim.duracion):
        alimentar(monitor, sim.leer(t))
    return monitor, gestor


class EjecutorSimulacion(threading.Thread):
    """Reproduce la simulación a velocidad ajustable para el tablero en vivo."""

    def __init__(self, simulador, monitor, velocidad=30):
        super().__init__(daemon=True)
        self.sim = simulador
        self.monitor = monitor
        self.velocidad = velocidad
        self.pausado = False
        self.terminado = False

    def run(self):
        t = 0
        while t < self.sim.duracion:
            if self.pausado:
                time.sleep(0.1)
                continue
            for _ in range(max(1, round(self.velocidad * 0.1))):
                alimentar(self.monitor, self.sim.leer(t))
                t += 1
                if t >= self.sim.duracion:
                    break
            time.sleep(0.1)
        self.terminado = True


def exportar_csv(cfg, escenario, ruta):
    """Escribe las lecturas simuladas con el mismo formato que acepta `analizar`."""
    sim = Simulador(cfg, escenario)
    ids = [e["id"] for e in cfg["estaciones"]]
    ruta_bitacora = ruta.rsplit(".", 1)[0] + ".bitacora.csv"
    with open(ruta, "w", newline="", encoding="utf-8") as f, \
            open(ruta_bitacora, "w", newline="", encoding="utf-8") as fb:
        w, wb = csv.writer(f), csv.writer(fb)
        w.writerow(["t", *ids, "q_entrada", "q_salida"])
        wb.writerow(["t", "descripcion"])
        for t in range(sim.duracion):
            l = sim.leer(t)
            w.writerow([t, *(l.presiones[e] for e in ids), l.q_entrada, l.q_salida])
            for op in l.operaciones:
                wb.writerow([t, op["descripcion"]])
    return ruta_bitacora


def analizar_csv(cfg, ruta, ruta_bitacora=None):
    """Procesa un histórico SCADA (1 muestra/s) y devuelve el gestor con los resultados."""
    monitor, gestor = crear_sistema(cfg)
    operaciones = {}
    if ruta_bitacora:
        with open(ruta_bitacora, encoding="utf-8") as fb:
            for fila in csv.DictReader(fb):
                operaciones.setdefault(int(float(fila["t"])), []).append(fila["descripcion"])
    ids = [e["id"] for e in cfg["estaciones"]]
    with open(ruta, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            t = int(float(fila["t"]))
            for desc in operaciones.get(t, []):
                monitor.registrar_operacion(t, desc)
            monitor.procesar(t, {e: float(fila[e]) for e in ids}, float(fila["q_entrada"]), float(fila["q_salida"]))
    return monitor, gestor
