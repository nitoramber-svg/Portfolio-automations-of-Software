"""Gestión de incidentes: alta, despacho de brigadas, seguimiento y cierre."""
from collections import deque
from statistics import fmean

TIPOS = {
    "toma_abrupta": "Toma clandestina (apertura súbita)",
    "extraccion_gradual": "Extracción gradual / toma de bajo flujo",
}

# acción -> (estados desde los que se permite, estado resultante)
ACCIONES = {
    "despachar": ({"nuevo"}, "despachado"),
    "en_sitio": ({"despachado"}, "en_sitio"),
    "cerrar_confirmada": ({"nuevo", "despachado", "en_sitio"}, "cerrado"),
    "cerrar_falsa": ({"nuevo", "despachado", "en_sitio"}, "cerrado"),
}


class ErrorAccion(Exception):
    pass


class GestorIncidentes:
    PERSISTIR_CADA_S = 60

    def __init__(self, cfg, almacen):
        self.cfg = cfg
        self.almacen = almacen
        self.precio = cfg["ducto"]["precio_mxn_litro"]
        self.incidentes = {}
        self.eventos = deque(maxlen=300)
        self._siguiente = 1
        self._ultimo_guardado = {}
        # Al reutilizar la base de un turno (reinicio, falla de energía) se continúa donde se quedó.
        for inc in almacen.incidentes():
            self.incidentes[inc["id"]] = inc
        self._siguiente = max(self.incidentes, default=0) + 1
        self.eventos.extend(almacen.eventos(limite=self.eventos.maxlen))

    # ---- eventos (bitácora general) ----
    def registrar_evento(self, t, tipo, descripcion, severidad="info"):
        ev = {"t": t, "tipo": tipo, "severidad": severidad, "descripcion": descripcion}
        self.eventos.append(ev)
        self.almacen.guardar_evento(ev)

    # ---- incidentes ----
    def _acceso_cercano(self, km):
        p = min(self.cfg["puntos_acceso"], key=lambda a: abs(a["km"] - km))
        return {"nombre": p["nombre"], "km": p["km"], "distancia_km": round(abs(p["km"] - km), 1)}

    def _brigada_cercana(self, km):
        b = min(self.cfg["brigadas"], key=lambda b: abs(b["base_km"] - km))
        r = self.cfg["respuesta"]
        eta = r["alistamiento_min"] + abs(b["base_km"] - km) / r["velocidad_brigada_km_h"] * 60
        return {"id": b["id"], "nombre": b["nombre"], "eta_min": round(eta)}

    def _guardar(self, inc):
        self.almacen.guardar_incidente(inc)
        self._ultimo_guardado[inc["id"]] = inc["notas"][-1]["t"] if inc["notas"] else 0

    def crear(self, t, tipo, km, incertidumbre_km, t_inicio_est, metodo, confianza, caudal_m3h=None, volumen_m3=0.0):
        inc = {
            "id": self._siguiente,
            "tipo": tipo,
            "tipo_texto": TIPOS[tipo],
            "estado": "nuevo",
            "confianza": confianza,
            "km": round(km, 2),
            "incertidumbre_km": round(incertidumbre_km, 2),
            "km_perfil": None,
            "metodo": metodo,
            "t_inicio_est": t_inicio_est,
            "t_deteccion": t,
            "t_confirmacion": t if confianza == "alta" else None,
            "caudal_m3h": caudal_m3h,
            "volumen_m3": volumen_m3,
            "extrayendo": True,
            "acceso": self._acceso_cercano(km),
            "brigada_sugerida": self._brigada_cercana(km),
            "brigada": None,
            "t_despacho": None,
            "t_cierre": None,
            "resultado": None,
            "notas": [],
        }
        self._siguiente += 1
        self.incidentes[inc["id"]] = inc
        self.anotar(inc["id"], t, f"Detectado por {metodo} en km {km:.1f} ± {incertidumbre_km:.1f}.")
        self.registrar_evento(
            t, "incidente",
            f"Incidente #{inc['id']}: {inc['tipo_texto']} en km {km:.1f}. "
            f"Acceso más cercano: {inc['acceso']['nombre']}.",
            "alta",
        )
        return inc

    def anotar(self, id_, t, texto):
        inc = self.incidentes[id_]
        inc["notas"].append({"t": t, "texto": texto})
        self._guardar(inc)

    def confirmar(self, id_, t, caudal_m3h, km_perfil):
        inc = self.incidentes[id_]
        inc["confianza"] = "alta"
        inc["t_confirmacion"] = t
        inc["extrayendo"] = True
        inc["caudal_m3h"] = round(caudal_m3h, 1)
        inc["km_perfil"] = None if km_perfil is None else round(km_perfil, 1)
        extra = f"; el perfil de presión la ubica en km {km_perfil:.1f}" if km_perfil is not None else ""
        self.anotar(id_, t, f"Confirmado por balance volumétrico: faltante de {caudal_m3h:.1f} m³/h{extra}.")
        self.registrar_evento(t, "confirmacion", f"Incidente #{id_} confirmado: faltante de {caudal_m3h:.1f} m³/h.", "alta")

    def acumular(self, id_, t, delta_m3, caudal_m3h):
        inc = self.incidentes[id_]
        inc["volumen_m3"] = max(0.0, inc["volumen_m3"] + delta_m3)
        if inc["t_confirmacion"] is not None:
            inc["caudal_m3h"] = round(caudal_m3h, 1)
        if t - self._ultimo_guardado.get(id_, 0) >= self.PERSISTIR_CADA_S:
            self.almacen.guardar_incidente(inc)
            self._ultimo_guardado[id_] = t

    def extraccion_detenida(self, id_, t):
        inc = self.incidentes[id_]
        inc["extrayendo"] = False
        inc["caudal_m3h"] = 0.0
        self.anotar(id_, t, f"El balance volumétrico se normalizó: la extracción cesó. Volumen estimado {inc['volumen_m3'] * 1000:,.0f} L.")
        self.registrar_evento(t, "info", f"Incidente #{id_}: la extracción cesó.", "media")

    def sin_confirmacion(self, id_, t):
        inc = self.incidentes[id_]
        inc["extrayendo"] = False
        self.anotar(id_, t, "Sin faltante volumétrico sostenido: posible toma cerrada de inmediato o intento fallido. Verificar en campo.")

    def aplicar_accion(self, id_, accion, t, comentario="", usuario=None):
        if id_ not in self.incidentes:
            raise ErrorAccion(f"No existe el incidente #{id_}.")
        if accion not in ACCIONES:
            raise ErrorAccion(f"Acción desconocida: {accion}.")
        inc = self.incidentes[id_]
        permitidos, nuevo = ACCIONES[accion]
        if inc["estado"] not in permitidos:
            raise ErrorAccion(f"No se puede '{accion}' un incidente en estado '{inc['estado']}'.")
        inc["estado"] = nuevo
        if accion == "despachar":
            inc["brigada"] = inc["brigada_sugerida"]
            inc["t_despacho"] = t
            texto = f"Despachada {inc['brigada']['nombre']} (ETA {inc['brigada']['eta_min']} min) vía {inc['acceso']['nombre']}."
        elif accion == "en_sitio":
            texto = "Brigada en sitio."
        else:
            inc["t_cierre"] = t
            inc["resultado"] = "Toma confirmada y sellada" if accion == "cerrar_confirmada" else "Falsa alarma"
            texto = f"Cerrado: {inc['resultado']}."
        if comentario:
            texto += f" Comentario: {comentario[:500]}"
        if usuario:
            texto += f" (por {usuario})"
        self.anotar(id_, t, texto)
        self.registrar_evento(t, "gestion", f"Incidente #{id_}: {texto}")
        return inc

    # ---- vistas ----
    def lista(self):
        salida = []
        for inc in sorted(self.incidentes.values(), key=lambda i: -i["id"]):
            litros = inc["volumen_m3"] * 1000
            salida.append({**inc, "litros": round(litros), "perdida_mxn": round(litros * self.precio)})
        return salida

    def kpis(self):
        incs = list(self.incidentes.values())
        litros = sum(i["volumen_m3"] for i in incs) * 1000
        tiempos = [i["t_deteccion"] - i["t_inicio_est"] for i in incs if i["t_inicio_est"] is not None]
        return {
            "activos": sum(1 for i in incs if i["estado"] != "cerrado"),
            "total": len(incs),
            "litros": round(litros),
            "perdida_mxn": round(litros * self.precio),
            "deteccion_media_s": round(fmean(tiempos)) if tiempos else None,
        }
