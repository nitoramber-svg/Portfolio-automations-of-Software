"""Ingesta de lecturas en tiempo real desde fuentes reales (HTTP, OPC-UA).

Las lecturas reales llegan por etiqueta, con marca de tiempo propia,
desordenadas, duplicadas, con retrasos o con huecos. El monitor necesita
exactamente una muestra completa por segundo. Esta capa traduce:

- valida etiqueta, valor, rango físico y marca de tiempo;
- agrupa por segundo y espera `latencia_s` a que lleguen las rezagadas
  (marca de agua por tiempo de evento, así también sirve para reproducir
  históricos a velocidad acelerada);
- rellena huecos cortos con el último valor. Si el hueco es largo deja de
  alimentar al monitor y, al volver los datos, reinicia los detectores para
  no confundir el salto con una toma.
"""
import math
import threading
import time
from collections import Counter, defaultdict


class Ingesta:
    def __init__(self, cfg, monitor, latencia_s=3, max_hueco_s=10, verificar_reloj=True,
                 tolerancia_reloj_s=5, reloj=time.time, inicio=None, al_fijar_inicio=None):
        d = cfg["ducto"]
        self.monitor = monitor
        self.latencia_s = latencia_s
        self.max_hueco_s = max_hueco_s
        self.verificar_reloj = verificar_reloj
        self.tolerancia_reloj_s = tolerancia_reloj_s
        self.reloj = reloj
        self.estaciones = [e["id"] for e in cfg["estaciones"]]
        self.etiquetas = self.estaciones + ["q_entrada", "q_salida"]
        q0 = d["caudal_nominal_m3_h"]
        self.rangos = {e: (-1.0, 1.5 * d["presion_entrada_bar"]) for e in self.estaciones}
        self.rangos["q_entrada"] = self.rangos["q_salida"] = (-0.5 * q0, 3.0 * q0)

        # Segundo epoch que corresponde a t = 0 del monitor. Se conserva entre reinicios
        # para que los tiempos de los incidentes guardados sigan siendo válidos.
        self.inicio = inicio
        self._al_fijar_inicio = al_fijar_inicio
        self._cubetas = defaultdict(dict)  # segundo -> etiqueta -> [suma, n]
        self._operaciones = defaultdict(list)
        self._ultimo = {}  # etiqueta -> (valor, segundo)
        self._estado = {e: "sin_datos" for e in self.etiquetas}
        self._siguiente = None
        self._max_ts = None
        self._ultima_recepcion = None
        self._alimentando = False
        self._interrumpido = False
        self.enlace = "esperando"
        self.aceptadas = Counter()
        self.rechazos = Counter()
        self.rellenos = Counter()
        self.segundos_emitidos = 0
        self.segundos_omitidos = 0
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- entrada
    def _validar(self, etiqueta, ts, valor):
        if etiqueta not in self.rangos:
            return "etiqueta_desconocida"
        if isinstance(valor, bool):
            return "valor_invalido"
        try:
            valor, ts = float(valor), float(ts)
        except (TypeError, ValueError):
            return "valor_invalido"
        if not math.isfinite(valor):
            return "valor_invalido"
        if not math.isfinite(ts) or ts <= 0:
            return "marca_tiempo_invalida"
        lo, hi = self.rangos[etiqueta]
        if not lo <= valor <= hi:
            return "fuera_de_rango"
        if self.verificar_reloj and ts > self.reloj() + self.tolerancia_reloj_s:
            return "marca_tiempo_futura"
        return None

    def recibir(self, etiqueta, ts, valor):
        """Registra una lectura. Devuelve None si se aceptó o el motivo del rechazo."""
        motivo = self._validar(etiqueta, ts, valor)
        with self.lock:
            if motivo is None and self._siguiente is not None and int(float(ts)) < self._siguiente:
                motivo = "tardia"
            if motivo:
                self.rechazos[motivo] += 1
                return motivo
            ts = float(ts)
            celda = self._cubetas[int(ts)].setdefault(etiqueta, [0.0, 0])
            celda[0] += float(valor)
            celda[1] += 1
            self._max_ts = ts if self._max_ts is None else max(self._max_ts, ts)
            self._ultima_recepcion = self.reloj()
            self.aceptadas[etiqueta] += 1
            return None

    def recibir_bitacora(self, ts, descripcion):
        if not isinstance(descripcion, str) or not descripcion.strip() or len(descripcion) > 500:
            return "descripcion_invalida"
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return "marca_tiempo_invalida"
        with self.lock:
            seg = int(ts) if self._siguiente is None else max(int(ts), self._siguiente)
            self._operaciones[seg].append(descripcion.strip())
        return None

    # ----------------------------------------------------------------- salida
    def avanzar(self):
        """Entrega al monitor todos los segundos que ya no esperan rezagadas."""
        emitidos = 0
        with self.lock:
            self._revisar_enlace()
            if self._max_ts is None:
                return 0
            limite = math.floor(self._max_ts - self.latencia_s) - 1
            if self._siguiente is None:
                self._siguiente = min(self._cubetas)
            while self._siguiente <= limite:
                self._saltar_hueco_largo(limite)
                if self._siguiente > limite:
                    break
                self._emitir(self._siguiente)
                self._siguiente += 1
                emitidos += 1
        return emitidos

    def _saltar_hueco_largo(self, limite):
        """Tras una caída larga del enlace no recorre segundo por segundo el vacío."""
        s = self._siguiente
        if s in self._cubetas or any(s - seg <= self.max_hueco_s for _, seg in self._ultimo.values()):
            return
        futuros = [k for k in self._cubetas if k >= s]
        destino = min(futuros) if futuros else limite + 1
        if destino > s:
            self.segundos_omitidos += destino - s
            if self._alimentando:
                self._interrumpido = True
            for e in self.etiquetas:
                self._cambiar_estado(e, "sin_datos", s)
            for seg in [k for k in self._operaciones if k < destino]:
                self._operaciones[destino].extend(self._operaciones.pop(seg))
            self._siguiente = destino

    def _emitir(self, s):
        cubeta = self._cubetas.pop(s, {})
        valores, faltantes = {}, []
        for e in self.etiquetas:
            if e in cubeta:
                suma, n = cubeta[e]
                valores[e] = suma / n
                self._ultimo[e] = (valores[e], s)
                estado = "ok"
            elif e in self._ultimo and s - self._ultimo[e][1] <= self.max_hueco_s:
                valores[e] = self._ultimo[e][0]
                self.rellenos[e] += 1
                estado = "relleno"
            else:
                faltantes.append(e)
                estado = "sin_datos"
            self._cambiar_estado(e, estado, s)
        operaciones = self._operaciones.pop(s, [])
        if faltantes:
            self.segundos_omitidos += 1
            if self._alimentando:
                self._interrumpido = True
            self._operaciones[s + 1].extend(operaciones)
            return
        if self.inicio is None:
            self.inicio = s
            if self._al_fijar_inicio:
                self._al_fijar_inicio(s)
        t = s - self.inicio
        if self._interrumpido:
            self.monitor.reanudar(t)
            self._interrumpido = False
        self._alimentando = True
        for desc in operaciones:
            self.monitor.registrar_operacion(t, desc)
        self.monitor.procesar(t, {e: valores[e] for e in self.estaciones}, valores["q_entrada"], valores["q_salida"])
        self.segundos_emitidos += 1

    # ------------------------------------------------------------- vigilancia
    def _evento(self, tipo, texto, severidad="media"):
        with self.monitor.lock:
            self.monitor.gestor.registrar_evento(self.monitor.t, tipo, texto, severidad)

    def _cambiar_estado(self, etiqueta, estado, s):
        anterior = self._estado[etiqueta]
        self._estado[etiqueta] = estado
        if etiqueta in self.monitor.estado_estaciones:
            with self.monitor.lock:
                if estado == "sin_datos":
                    self.monitor.estado_estaciones[etiqueta] = "sin_datos"
                elif self.monitor.estado_estaciones[etiqueta] == "sin_datos":
                    self.monitor.estado_estaciones[etiqueta] = "normal"
        if not self._alimentando:
            return
        if estado == "sin_datos" and anterior != "sin_datos":
            self._evento("datos", f"La etiqueta {etiqueta} dejó de reportar por más de {self.max_hueco_s} s: "
                                  f"se suspende la detección hasta que regrese.")
        elif anterior == "sin_datos" and estado == "ok":
            self._evento("datos", f"La etiqueta {etiqueta} volvió a reportar.", "info")

    def _revisar_enlace(self):
        if self._ultima_recepcion is None:
            return
        silencio = self.reloj() - self._ultima_recepcion
        if silencio > self.max_hueco_s and self.enlace != "caido":
            self.enlace = "caido"
            self._evento("datos", f"Sin lecturas del SCADA desde hace {silencio:.0f} s: revisar el enlace de comunicaciones.", "alta")
        elif silencio <= self.max_hueco_s and self.enlace != "ok":
            if self.enlace == "caido":
                self._evento("datos", "Enlace con el SCADA restablecido.", "info")
            self.enlace = "ok"

    def calidad(self):
        with self.lock:
            retraso = None if self._ultima_recepcion is None else round(self.reloj() - self._ultima_recepcion, 1)
            return {
                "enlace": self.enlace,
                "segundos_sin_datos": retraso,
                "latencia_s": self.latencia_s,
                "etiquetas": {e: {"estado": self._estado[e], "aceptadas": self.aceptadas[e], "rellenos": self.rellenos[e]}
                              for e in self.etiquetas},
                "aceptadas": sum(self.aceptadas.values()),
                "rechazos": dict(self.rechazos),
                "segundos_emitidos": self.segundos_emitidos,
                "segundos_omitidos": self.segundos_omitidos,
            }


class BombaIngesta(threading.Thread):
    """Hace avanzar la ingesta periódicamente aunque no lleguen lecturas nuevas."""

    def __init__(self, ingesta, periodo_s=0.2):
        super().__init__(daemon=True)
        self.ingesta = ingesta
        self.periodo_s = periodo_s

    def run(self):
        while True:
            self.ingesta.avanzar()
            time.sleep(self.periodo_s)
