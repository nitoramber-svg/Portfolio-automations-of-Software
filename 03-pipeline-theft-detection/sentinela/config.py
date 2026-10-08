"""Carga y validación de la configuración del ducto y de los escenarios."""
import json
import os

DIR_DATOS = os.path.join(os.path.dirname(__file__), "datos")
DUCTO_DEMO = os.path.join(DIR_DATOS, "ducto_demo.json")
ESCENARIO_DEMO = os.path.join(DIR_DATOS, "escenario_demo.json")


def _leer_json(ruta):
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def cargar_ducto(ruta=None):
    cfg = _leer_json(ruta or DUCTO_DEMO)
    estaciones = sorted(cfg["estaciones"], key=lambda e: e["km"])
    if len(estaciones) < 3:
        raise ValueError("Se requieren al menos 3 estaciones de presión.")
    L = cfg["ducto"]["longitud_km"]
    if estaciones[0]["km"] != 0 or estaciones[-1]["km"] != L:
        raise ValueError("Debe haber estaciones en ambas terminales (km 0 y km L).")
    cfg["estaciones"] = estaciones
    return cfg


def cargar_escenario(ruta=None):
    esc = _leer_json(ruta or ESCENARIO_DEMO)
    esc["eventos"].sort(key=lambda e: e["t"])
    return esc
