"""Simulador hidráulico simplificado de un ducto con tomas clandestinas.

Genera lecturas SCADA sintéticas (presión por estación y caudal en las
terminales) para probar y demostrar el sistema sin acceso a datos reales.

Modelo (deliberadamente simple; ver README):
- Perfil de presión lineal entre terminales (resistencia linealizada).
- Una toma de caudal q en el km x abate el perfil en forma de triángulo con
  vértice en x, sube el caudal de entrada q·(L-x)/L y baja el de salida q·x/L.
- Una apertura súbita además emite una onda de presión negativa (Joukowsky)
  que viaja a la velocidad del sonido en el producto y se atenúa.
- Las maniobras en terminales cambian la presión de frontera; el efecto llega
  a cada punto con su retardo de propagación.
"""
import math
import random
from dataclasses import dataclass, field


def _rampa(x):
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x


@dataclass
class Lectura:
    t: int
    presiones: dict
    q_entrada: float
    q_salida: float
    operaciones: list = field(default_factory=list)


class Simulador:
    SUBIDA_S = 3.0  # tiempo de subida de un frente de presión

    def __init__(self, cfg, escenario):
        d = cfg["ducto"]
        self.L = d["longitud_km"]
        self.v = d["velocidad_onda_m_s"] / 1000.0  # km/s
        self.p_in0 = d["presion_entrada_bar"]
        self.p_out0 = d["presion_salida_bar"]
        self.q0 = d["caudal_nominal_m3_h"]
        self.dp0 = self.p_in0 - self.p_out0
        self.r = self.dp0 / (self.L * self.q0)  # bar / (km · m3/h)
        area = math.pi * (d["diametro_m"] / 2) ** 2
        # Joukowsky ΔP = ρ·a·Δv; la mitad de la onda viaja en cada sentido.
        self.k_onda = d["densidad_kg_m3"] * d["velocidad_onda_m_s"] / (area * 3600) / 1e5 / 2
        self.estaciones = [(e["id"], e["km"]) for e in cfg["estaciones"]]
        self.esc = escenario
        self.rng = random.Random(escenario.get("semilla", 0))
        eventos = escenario["eventos"]
        self.operaciones = [e for e in eventos if e["tipo"] == "operacion"]
        self.tomas = [e for e in eventos if e["tipo"] == "toma"]
        self.fallas = [e for e in eventos if e["tipo"] == "falla_sensor"]
        self.duracion = escenario["duracion_s"]

    def _frontera(self, extremo, x, t):
        x0 = 0.0 if extremo == "entrada" else self.L
        total = 0.0
        for op in self.operaciones:
            if op["extremo"] == extremo:
                llegada = op["t"] + abs(x - x0) / self.v
                total += op["delta_bar"] * _rampa((t - llegada) / self.SUBIDA_S)
        return total

    def _caudal_toma(self, toma, t):
        dt = t - toma["t"]
        if dt <= 0 or dt >= toma["duracion_s"]:
            return 0.0
        apertura = toma.get("apertura_s") or self.SUBIDA_S
        return toma["caudal_m3h"] * _rampa(dt / apertura)

    def _caudal_terminal(self, x, t):
        dp = (self.p_in0 + self._frontera("entrada", x, t)) - (self.p_out0 + self._frontera("salida", x, t))
        return self.q0 * math.sqrt(max(dp, 0.0) / self.dp0)

    def leer(self, t):
        esc = self.esc
        presiones = {}
        for est, x in self.estaciones:
            p_in = self.p_in0 + self._frontera("entrada", x, t)
            p_out = self.p_out0 + self._frontera("salida", x, t)
            p = p_in + (p_out - p_in) * x / self.L
            for toma in self.tomas:
                xt = toma["km"]
                tr = t - abs(x - xt) / self.v  # tiempo retardado en la toma
                forma = x * (self.L - xt) if x <= xt else xt * (self.L - x)
                p -= self.r * self._caudal_toma(toma, tr) * forma / self.L
                if not toma.get("apertura_s"):
                    amp = self.k_onda * toma["caudal_m3h"] * math.exp(-abs(x - xt) / esc["atenuacion_km"])
                    for t_frente, signo in ((toma["t"], -1), (toma["t"] + toma["duracion_s"], 1)):
                        dt = tr - t_frente
                        if dt > 0:
                            p += signo * amp * _rampa(dt / self.SUBIDA_S) * math.exp(-dt / esc["decaimiento_onda_s"])
            for f in self.fallas:
                if f["estacion"] == est and f["t"] <= t < f["t"] + f["duracion_s"]:
                    p += f["delta_bar"]
            presiones[est] = round(p + self.rng.gauss(0, esc["ruido_presion_bar"]), 4)

        q_in = self._caudal_terminal(0.0, t)
        q_out = self._caudal_terminal(self.L, t)
        for toma in self.tomas:
            xt = toma["km"]
            q_in += self._caudal_toma(toma, t - xt / self.v) * (self.L - xt) / self.L
            q_out -= self._caudal_toma(toma, t - (self.L - xt) / self.v) * xt / self.L
        q_in += self.rng.gauss(0, esc["ruido_caudal_m3h"])
        q_out += esc.get("sesgo_medidor_salida_m3h", 0.0) + self.rng.gauss(0, esc["ruido_caudal_m3h"])

        ops = [op for op in self.operaciones if op["t"] == t and op.get("registrada", True)]
        return Lectura(t, presiones, round(q_in, 3), round(q_out, 3), ops)
