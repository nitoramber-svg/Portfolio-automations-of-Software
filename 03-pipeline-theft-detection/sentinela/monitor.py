"""Monitor en tiempo real: recibe lecturas SCADA, fusiona los tres métodos y
decide qué es una toma, qué es una maniobra operativa y qué es una falla de sensor."""
import threading
from collections import deque

from .deteccion import (BalanceVolumetrico, DetectorOnda, PerfilPresion,
                        localizar_por_onda, localizar_por_perfil)


class Monitor:
    def __init__(self, cfg, gestor):
        self.cfg = cfg
        self.det = cfg["deteccion"]
        self.gestor = gestor
        self.L = cfg["ducto"]["longitud_km"]
        self.v_kms = cfg["ducto"]["velocidad_onda_m_s"] / 1000.0
        self.km = {e["id"]: e["km"] for e in cfg["estaciones"]}
        self.ondas = {e: DetectorOnda(e, self.det["npw_umbral_bar"], self.det["npw_refractario_s"]) for e in self.km}
        self.balance = BalanceVolumetrico(self.det["cusum_k_m3h"], self.det["cusum_h"],
                                          self.det["calibracion_s"], self.det["ventana_balance_s"],
                                          self.det.get("persistencia_balance_s", round(2 * self.L / self.v_kms) + 30))
        # Además de las estaciones, el perfil guarda promedios por minuto del caudal de
        # entrada y del desbalance para estimar la amplitud r·q en la localización.
        self.perfil = PerfilPresion(list(self.km) + ["_q_entrada", "_desbalance"])
        self.bitacora = []
        self.estado_estaciones = {e: "normal" for e in self.km}
        self.series = deque(maxlen=900)
        self.lock = threading.RLock()
        self.t = 0
        self._cluster = None
        self._ventana_cluster = self.L / self.v_kms + 5
        self._vinculado = None  # incidente al que se atribuye el faltante volumétrico

    # ---------------------------------------------------------------- entrada
    def registrar_operacion(self, t, descripcion):
        """Maniobra capturada en la bitácora operativa del SCADA."""
        with self.lock:
            self.bitacora.append({"t": t, "descripcion": descripcion})
            self.balance.suprimir(t + self.det["supresion_operativa_s"])
            self.gestor.registrar_evento(t, "operacion", f"Bitácora: {descripcion}")

    def reanudar(self, t):
        """Tras un hueco largo de datos: un salto de presión no debe leerse como onda."""
        with self.lock:
            self.ondas = {e: DetectorOnda(e, self.det["npw_umbral_bar"], self.det["npw_refractario_s"]) for e in self.km}
            self._cluster = None
            self.balance.suprimir(t + self.det["supresion_operativa_s"])
            self.gestor.registrar_evento(
                t, "datos",
                "Se reanudó el flujo de datos tras un hueco: detectores de onda reiniciados y balance en pausa "
                "para no confundir el salto con una toma.",
                "media",
            )

    def procesar(self, t, presiones, q_in, q_out):
        with self.lock:
            self.t = t
            for est, p in presiones.items():
                llegada = self.ondas[est].procesar(t, p)
                if llegada:
                    self._agregar_llegada(llegada)
            if self._cluster and t > self._cluster["inicio"] + self._ventana_cluster + 15:
                self._evaluar_cluster()
            senal = self.balance.procesar(t, q_in, q_out)
            extra = {"_q_entrada": q_in}
            if not self.balance.calibrando:
                extra["_desbalance"] = self.balance.ultimo
            self.perfil.procesar(t, {**presiones, **extra})
            if senal == "alarma":
                self._alarma_balance(t)
            elif senal == "normalizado":
                self._balance_normalizado(t)
            elif senal == "transitorio":
                self.gestor.registrar_evento(
                    t, "operativo",
                    "Desbalance entrada/salida que no persistió: probable re-empaque de la línea por una maniobra "
                    "no registrada. Descartado como toma.")
            self._seguir_vinculado(t)
            self.series.append((t, presiones, q_in, q_out, None if self.balance.calibrando else self.balance.ultimo))

    # ------------------------------------------------- onda de presión negativa
    def _agregar_llegada(self, llegada):
        if self._cluster and llegada["t"] - self._cluster["inicio"] <= self._ventana_cluster:
            self._cluster["llegadas"].append(llegada)
            return
        if self._cluster:
            self._evaluar_cluster()
        self._cluster = {"inicio": llegada["t"], "llegadas": [llegada]}

    def _evaluar_cluster(self):
        c, self._cluster = self._cluster, None
        llegadas = sorted(c["llegadas"], key=lambda l: l["t"])
        t = self.t
        if len(llegadas) == 1:
            l = llegadas[0]
            self.estado_estaciones[l["estacion"]] = "revisar"
            self.gestor.registrar_evento(
                t, "falla_sensor",
                f"Escalón aislado de -{l['caida_bar']:.2f} bar en {l['estacion']} sin eco en estaciones vecinas: "
                f"probable falla del transmisor. Se genera orden de mantenimiento; no se despacha brigada.",
                "media",
            )
            return
        x, t0, rmse = localizar_por_onda([(self.km[l["estacion"]], l["t"], l["caida_bar"]) for l in llegadas],
                                         self.L, self.v_kms)
        estaciones = ", ".join(l["estacion"] for l in llegadas)
        op = next((o for o in self.bitacora if abs(o["t"] - t0) <= 60), None)
        tol = self.det["tolerancia_extremo_km"]
        if op or x <= tol or x >= self.L - tol:
            motivo = f"coincide con bitácora: «{op['descripcion']}»" if op else \
                "originado en una terminal; maniobra no registrada en bitácora (avisar al centro de control)"
            self.gestor.registrar_evento(t, "operativo", f"Transitorio en km {x:.1f} ({estaciones}) {motivo}. Descartado como toma.")
            return
        if rmse > self.det["npw_rmse_max_s"]:
            self.gestor.registrar_evento(t, "no_concluyente",
                                         f"Frente de presión incoherente (error {rmse:.1f} s) en {estaciones}. Se mantiene vigilancia.",
                                         "media")
            return
        inc = self.gestor.crear(
            t, "toma_abrupta", x, max(0.3, 2 * rmse * self.v_kms), t0,
            f"onda de presión negativa ({len(llegadas)} estaciones)", "media",
        )
        self._vinculado = {"id": inc["id"], "limite": t + self.det["ventana_confirmacion_s"], "confirmado": False}

    # ------------------------------------------------------ balance y perfil
    def _localizar_perfil(self, t):
        hasta = self.balance.t_ultimo_cero
        deltas = []
        for est, km in self.km.items():
            ref = self.perfil.promedio(est, hasta - 360, hasta)
            act = self.perfil.promedio(est, t - 180, t)
            if ref is not None and act is not None:
                deltas.append((km, ref - act))
        if len(deltas) < 3:
            return None
        x, D, _ = localizar_por_perfil(deltas, self.L, amplitud=self._amplitud_perfil(t, hasta))
        return x if D > 0 else None

    def _amplitud_perfil(self, t, hasta):
        """r·Δq: resistencia hidráulica actual (ΔP terminales / L·Q) por el aumento del faltante."""
        p = self.perfil.promedio
        extremos = sorted(self.km, key=self.km.get)
        p_ini, p_fin = p(extremos[0], t - 180, t), p(extremos[-1], t - 180, t)
        q_in = p("_q_entrada", t - 180, t)
        d_act = p("_desbalance", t - 180, t)
        d_ref = p("_desbalance", hasta - 360, hasta) or 0.0
        if None in (p_ini, p_fin, q_in, d_act) or q_in <= 0:
            return None
        return (p_ini - p_fin) / (self.L * q_in) * (d_act - d_ref)

    def _alarma_balance(self, t):
        caudal = self.balance.promedio_reciente(t - self.balance.t_ultimo_cero)
        km_perfil = self._localizar_perfil(t)
        v = self._vinculado
        if v and not v["confirmado"] and t <= v["limite"]:
            self.gestor.confirmar(v["id"], t, caudal, km_perfil)
            v["confirmado"] = True
            return
        # Volumen ya extraído desde que el CUSUM empezó a acumular: Σd = S + k·Δt.
        dt = t - self.balance.t_ultimo_cero
        volumen_previo = (self.balance.S + self.balance.k * dt) / 3600
        # Una toma abrupta pequeña puede tardar más que la ventana de confirmación en
        # acumular evidencia volumétrica: si hay un incidente de onda abierto en el mismo
        # sitio, se confirma ese en lugar de duplicarlo.
        previo = self._incidente_onda_compatible(t, km_perfil)
        if previo:
            self.gestor.confirmar(previo["id"], t, caudal, km_perfil)
            self.gestor.acumular(previo["id"], t, volumen_previo, caudal)
            self._vinculado = {"id": previo["id"], "limite": None, "confirmado": True}
            return
        km = km_perfil if km_perfil is not None else self.L / 2
        incert = self.det["perfil_incertidumbre_km"] if km_perfil is not None else self.L / 2
        inc = self.gestor.crear(
            t, "extraccion_gradual", km, incert, self.balance.t_ultimo_cero,
            "balance volumétrico + perfil de presión", "alta", round(caudal, 1), volumen_previo,
        )
        self._vinculado = {"id": inc["id"], "limite": None, "confirmado": True}

    def _incidente_onda_compatible(self, t, km_perfil, horizonte_s=7200, tolerancia_km=5.0):
        for inc in reversed(list(self.gestor.incidentes.values())):
            if t - inc["t_deteccion"] > horizonte_s:
                break
            if (inc["tipo"] == "toma_abrupta" and inc["t_confirmacion"] is None and inc["estado"] != "cerrado"
                    and (km_perfil is None or abs(km_perfil - inc["km"]) <= tolerancia_km)):
                return inc
        return None

    def _balance_normalizado(self, t):
        if self._vinculado and self._vinculado["confirmado"]:
            self.gestor.extraccion_detenida(self._vinculado["id"], t)
            self._vinculado = None

    def _seguir_vinculado(self, t):
        v = self._vinculado
        if not v or self.balance.calibrando:
            return
        if not v["confirmado"] and t > v["limite"]:
            self.gestor.sin_confirmacion(v["id"], t)
            self._vinculado = None
            return
        self.gestor.acumular(v["id"], t, self.balance.ultimo / 3600, self.balance.promedio)

    # ---------------------------------------------------------------- vistas
    def estado(self):
        ultimo = self.series[-1] if self.series else None
        return {
            "t": self.t,
            "presiones": ultimo[1] if ultimo else {},
            "q_entrada": ultimo[2] if ultimo else None,
            "q_salida": ultimo[3] if ultimo else None,
            "estaciones": self.estado_estaciones,
            "balance": {
                "calibrando": self.balance.calibrando,
                "sesgo": round(self.balance.sesgo, 2),
                "promedio_m3h": round(self.balance.promedio, 2),
                "cusum": round(self.balance.S, 1),
                "umbral": self.balance.h,
                "en_alarma": self.balance.en_alarma,
                "suprimido": self.t < self.balance.suprimido_hasta,
            },
            "kpis": self.gestor.kpis(),
        }

    def series_json(self, paso=2):
        datos = list(self.series)[::paso]
        return {
            "t": [d[0] for d in datos],
            "presiones": {e: [d[1][e] for d in datos] for e in self.km},
            "q_entrada": [d[2] for d in datos],
            "q_salida": [d[3] for d in datos],
            "desbalance": [d[4] for d in datos],
        }
