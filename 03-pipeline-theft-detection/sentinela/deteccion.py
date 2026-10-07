"""Algoritmos de detección y localización de tomas clandestinas.

Tres métodos complementarios, todos sobre datos que un SCADA ya entrega:

1. Onda de presión negativa (NPW): al perforar el ducto la presión cae de
   golpe y ese frente viaja en ambos sentidos a ~1 km/s. Comparando la hora
   de llegada a cada estación se triangula el km de la perforación.
2. Balance volumétrico con CUSUM: lo que entra menos lo que sale. Una
   extracción sostenida acumula evidencia aunque sea pequeña o lenta.
3. Perfil de presión: una extracción sostenida deforma el perfil
   estacionario en forma de triángulo con vértice en la toma. Localiza
   tomas de apertura lenta que no generan onda.
"""
import math
from collections import deque
from statistics import fmean


class DetectorOnda:
    """Detecta frentes de presión negativa en una estación y estima su hora de llegada."""

    ESPERA_CONFIRMACION_S = 8

    def __init__(self, estacion, umbral_bar, refractario_s):
        self.estacion = estacion
        self.umbral = umbral_bar
        self.refractario = refractario_s
        self._buf = deque(maxlen=90)
        self._pendiente = None  # (t_disparo, presión base)
        self._bloqueado_hasta = -math.inf

    def procesar(self, t, presion):
        self._buf.append((t, presion))
        if self._pendiente is not None:
            if t >= self._pendiente[0] + self.ESPERA_CONFIRMACION_S:
                return self._confirmar(t)
            return None
        if t < self._bloqueado_hasta or len(self._buf) < 60:
            return None
        valores = [p for _, p in self._buf]
        base = fmean(valores[-50:-15])
        caida = base - fmean(valores[-3:])
        # Solo frentes bruscos: justo antes del disparo la presión debe seguir en su
        # base. Una apertura gradual produce una rampa que no se puede cronometrar
        # con precisión; esas tomas las detectan el balance y el perfil.
        previa = base - fmean(valores[-14:-9])
        if caida > self.umbral and previa < 0.3 * caida:
            self._pendiente = (t, base)
        return None

    def _confirmar(self, t):
        t_disparo, base = self._pendiente
        self._pendiente = None
        self._bloqueado_hasta = t + self.refractario
        muestras = [m for m in self._buf if m[0] >= t_disparo - 15]
        caida = base - fmean(p for tt, p in muestras if tt > t - 5)
        if caida < 0.8 * self.umbral:
            return None  # pico pasajero, no un frente sostenido
        nivel = base - caida / 2
        # Último cruce descendente del 50 % tras el cual la señal ya no se recupera,
        # interpolado entre muestras para obtener resolución sub-segundo.
        i = len(muestras) - 1
        while i > 0 and muestras[i][1] < nivel:
            i -= 1
        if i == len(muestras) - 1:
            return None
        (ta, pa), (tb, pb) = muestras[i], muestras[i + 1]
        t_llegada = ta + (pa - nivel) / (pa - pb) * (tb - ta) if pa >= nivel else ta
        return {"estacion": self.estacion, "t": t_llegada, "caida_bar": caida}


def localizar_por_onda(llegadas, L, v_kms, paso_km=0.05):
    """Mínimos cuadrados ponderados sobre t_k = t0 + |x_k - x| / v.

    llegadas: lista de (km_estación, t_llegada, caída_bar). El error de tiempo
    es inversamente proporcional a la amplitud del frente, por eso cada
    estación pesa caída². Devuelve (km, t0, rmse_s).
    """
    pesos = [c * c for _, _, c in llegadas]
    suma_pesos = sum(pesos)
    mejor = None
    for i in range(int(round(L / paso_km)) + 1):
        x = i * paso_km
        residuos = [t - abs(k - x) / v_kms for k, t, _ in llegadas]
        t0 = sum(w * r for w, r in zip(pesos, residuos)) / suma_pesos
        costo = sum(w * (r - t0) ** 2 for w, r in zip(pesos, residuos)) / suma_pesos
        if mejor is None or costo < mejor[0]:
            mejor = (costo, x, t0)
    costo, x, t0 = mejor
    return x, t0, math.sqrt(costo)


def localizar_por_perfil(deltas, L, paso_km=0.05, amplitud=None):
    """Ajusta un triángulo con vértice en la toma a la caída de presión por estación.

    deltas: lista de (km_estación, caída_bar). Devuelve (km, caída_en_vértice, rmse).

    En el primer y último tramo la forma sola no distingue "toma pequeña lejos de la
    terminal" de "toma grande cerca": solo se ve un lado del triángulo. Si se conoce
    `amplitud` = r·q (resistencia hidráulica × caudal robado medido por el balance),
    la caída esperada es r·q·k·(L−x)/L antes de la toma y r·q·x·(L−k)/L después, lo
    que fija x sin ambigüedad.
    """
    def ajustar(con_amplitud):
        mejor = None
        for i in range(1, int(round(L / paso_km))):
            x = i * paso_km
            if con_amplitud:
                forma = [k * (L - x) / L if k <= x else x * (L - k) / L for k, _ in deltas]
                D = amplitud
            else:
                forma = [k / x if k <= x else (L - k) / (L - x) for k, _ in deltas]
                D = sum(g * d for g, (_, d) in zip(forma, deltas)) / sum(g * g for g in forma)
            costo = sum((d - D * g) ** 2 for g, (_, d) in zip(forma, deltas))
            if mejor is None or costo < mejor[0]:
                mejor = (costo, x, D * (x * (L - x) / L if con_amplitud else 1))
        costo, x, D = mejor
        return x, D, math.sqrt(costo / len(deltas))

    libre = ajustar(False)
    kms = sorted(k for k, _ in deltas)
    if amplitud and amplitud > 0 and len(kms) >= 3:
        fijo = ajustar(True)
        # En los tramos extremos manda el ajuste con caudal conocido; en el interior la
        # forma del triángulo basta y no depende de calibrar la resistencia hidráulica.
        if fijo[0] < kms[1] or fijo[0] > kms[-2]:
            return fijo
    return libre


class BalanceVolumetrico:
    """Balance entrada - salida con calibración de sesgo y CUSUM unilateral."""

    def __init__(self, k_m3h, h, calibracion_s, ventana_s, persistencia_s=250):
        self.k = k_m3h
        self.h = h
        # Un arranque de bomba no registrado desbalancea entrada y salida mientras la
        # línea se re-empaca (~2·L/v). Una toma persiste: se exige que el faltante
        # siga presente tras ese lapso antes de alarmar.
        self.persistencia_s = persistencia_s
        self._pendiente_desde = None
        self.calibracion_s = calibracion_s
        self.ventana_s = ventana_s
        self._sesgo = [0.0, 0]
        self.sesgo = 0.0
        self._ventana = deque(maxlen=ventana_s)
        self.S = 0.0
        self.ultimo = 0.0
        self.en_alarma = False
        self.t_ultimo_cero = 0
        self.suprimido_hasta = -math.inf
        self._t_normal = 0
        self.calibrando = True
        self._t_inicio = None  # la calibración cuenta desde la primera muestra, no desde t = 0

    @property
    def promedio(self):
        return fmean(self._ventana) if self._ventana else 0.0

    def promedio_reciente(self, n):
        """Promedio de las últimas n muestras (p. ej. desde que inició el faltante)."""
        muestras = list(self._ventana)[-max(1, n):]
        return fmean(muestras) if muestras else 0.0

    def suprimir(self, hasta):
        """Ignora transitorios de llenado/vaciado tras una maniobra registrada."""
        self.suprimido_hasta = max(self.suprimido_hasta, hasta)

    def procesar(self, t, q_in, q_out):
        bruto = q_in - q_out
        if self._t_inicio is None:
            self._t_inicio = t
        if t - self._t_inicio < self.calibracion_s:
            self._sesgo[0] += bruto
            self._sesgo[1] += 1
            self.sesgo = self._sesgo[0] / self._sesgo[1]
            return None
        self.calibrando = False
        d = bruto - self.sesgo
        self.ultimo = d
        self._ventana.append(d)
        if self.en_alarma:
            if self.promedio < self.k:
                self._t_normal += 1
                if self._t_normal >= self.ventana_s:
                    self.en_alarma = False
                    self.S = 0.0
                    self.t_ultimo_cero = t
                    return "normalizado"
            else:
                self._t_normal = 0
            return None
        if t < self.suprimido_hasta:
            self.S = 0.0
            self.t_ultimo_cero = t
            self._pendiente_desde = None
            return None
        self.S = max(0.0, self.S + d - self.k)
        if self.S == 0.0:
            self.t_ultimo_cero = t
        if self._pendiente_desde is None:
            if self.S > self.h:
                self._pendiente_desde = t
            return None
        if t - self._pendiente_desde < self.persistencia_s:
            return None
        self._pendiente_desde = None
        if self.promedio_reciente(120) > self.k:
            self.en_alarma = True
            self._t_normal = 0
            return "alarma"
        self.S = 0.0
        self.t_ultimo_cero = t
        return "transitorio"


class PerfilPresion:
    """Promedios por minuto de cada estación para comparar perfiles estacionarios."""

    def __init__(self, estaciones, historial_min=90):
        self._acum = {e: [0.0, 0] for e in estaciones}
        self._minuto = None
        self.historial = {e: deque(maxlen=historial_min) for e in estaciones}

    def procesar(self, t, presiones):
        minuto = int(t // 60)
        if self._minuto is None:
            self._minuto = minuto
        if minuto != self._minuto:
            fin = (self._minuto + 1) * 60
            for e, (s, n) in self._acum.items():
                if n:
                    self.historial[e].append((fin, s / n))
                self._acum[e] = [0.0, 0]
            self._minuto = minuto
        for e, p in presiones.items():
            self._acum[e][0] += p
            self._acum[e][1] += 1

    def promedio(self, estacion, desde, hasta):
        valores = [p for fin, p in self.historial[estacion] if desde < fin <= hasta]
        return fmean(valores) if valores else None
