"""Emisor de prueba: envía lecturas simuladas por HTTP como lo haría una pasarela
SCADA real, con desorden, retrasos, pérdidas y datos basura opcionales.

También sirve de referencia para quien integre un SCADA real: muestra el formato
de `POST /api/lecturas`, el token y el reintento (store-and-forward) si la red falla.
"""
import json
import random
import time
import urllib.error
import urllib.request
from collections import Counter

from .simulador import Simulador

BASURA = (
    {"etiqueta": "E9", "valor": 50.0},     # etiqueta desconocida
    {"etiqueta": "E2", "valor": 999.0},    # fuera de rango físico
    {"etiqueta": "E3", "valor": "NaN"},    # valor inválido
)


def _post(url, token, cuerpo):
    encabezados = {"Content-Type": "application/json"}
    if token:
        encabezados["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), headers=encabezados, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def emitir(cfg, escenario, url, token=None, velocidad=1.0, perdida=0.0, desorden=False, basura=False,
           semilla=1, salida=print):
    sim = Simulador(cfg, escenario)
    rng = random.Random(semilla)
    url = url.rstrip("/")
    inicio, inicio_mono = time.time(), time.monotonic()
    pendientes, retenidas = [], []
    ultimo_envio = time.monotonic()
    stats = Counter()

    def enviar():
        nonlocal pendientes, ultimo_envio
        ultimo_envio = time.monotonic()
        if not pendientes:
            return
        if desorden:
            rng.shuffle(pendientes)
        try:
            r = _post(url + "/api/lecturas", token, {"lecturas": pendientes})
        except urllib.error.HTTPError as e:
            raise SystemExit(f"El servidor rechazó el envío ({e.code}): {e.read().decode(errors='replace')}")
        except (urllib.error.URLError, OSError):
            stats["reintentos"] += 1
            pendientes = pendientes[-20000:]  # se conservan para reenviar cuando vuelva la red
            return
        stats["aceptadas"] += r["aceptadas"]
        stats["rechazadas"] += len(r["rechazadas"])
        pendientes = []

    for t in range(sim.duracion):
        espera = inicio_mono + t / velocidad - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        l = sim.leer(t)
        ts0 = inicio + t
        for op in l.operaciones:
            try:
                _post(url + "/api/bitacora", token, {"ts": ts0, "descripcion": op["descripcion"]})
            except (urllib.error.URLError, OSError):
                stats["bitacora_perdida"] += 1
        for etiqueta, valor in {**l.presiones, "q_entrada": l.q_entrada, "q_salida": l.q_salida}.items():
            if rng.random() < perdida:
                stats["perdidas_simuladas"] += 1
                continue
            lectura = {"etiqueta": etiqueta, "ts": round(ts0 + rng.uniform(0, 0.9), 3), "valor": valor}
            if desorden and rng.random() < 0.05:
                retenidas.append((t + rng.randint(1, 2), lectura))  # llega 1–2 s tarde
            else:
                pendientes.append(lectura)
        pendientes += [r for liberar, r in retenidas if liberar <= t]
        retenidas = [(liberar, r) for liberar, r in retenidas if liberar > t]
        if basura and rng.random() < 0.01:
            pendientes.append({**rng.choice(BASURA), "ts": ts0})
        if time.monotonic() - ultimo_envio >= 0.5:
            enviar()
        if t % 300 == 0:
            salida(f"[t={t:>5} s] aceptadas={stats['aceptadas']} rechazadas={stats['rechazadas']} "
                   f"perdidas_simuladas={stats['perdidas_simuladas']} reintentos={stats['reintentos']}")
    enviar()
    return stats
