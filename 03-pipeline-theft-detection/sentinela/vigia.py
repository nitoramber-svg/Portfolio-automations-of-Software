"""Vigía: proceso independiente que avisa al personal de guardia.

Corre en otra máquina (o al menos en otro proceso) y consulta al servidor cada N segundos:
- si el servidor no responde o reporta "degradado" N veces seguidas, avisa una sola vez,
  y vuelve a avisar cuando se restablece;
- cada incidente nuevo se notifica con su km y acceso, para que la guardia actúe aunque
  nadie esté mirando el tablero.

Canales: webhook genérico (Slack, Teams, Discord, n8n…: POST {"text": ...}) y Telegram.
"""
import json
import ssl
import time
import urllib.error
import urllib.request


def _peticion(url, token=None, cuerpo=None, inseguro=False, timeout=10):
    encabezados = {"Content-Type": "application/json"}
    if token:
        encabezados["Authorization"] = f"Bearer {token}"
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, headers=encabezados, method="POST" if datos else "GET")
    contexto = ssl._create_unverified_context() if inseguro else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=contexto) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def notificador_webhook(url):
    return lambda texto: _peticion(url, cuerpo={"text": texto})


def notificador_telegram(token_bot, chat_id):
    url = f"https://api.telegram.org/bot{token_bot}/sendMessage"
    return lambda texto: _peticion(url, cuerpo={"chat_id": chat_id, "text": texto})


class Vigia:
    def __init__(self, url, notificar, token=None, fallos_para_alertar=2, inseguro=False):
        self.url = url.rstrip("/")
        self.notificar = notificar
        self.token = token
        self.fallos_para_alertar = fallos_para_alertar
        self.inseguro = inseguro
        self._fallos = 0
        self._alertado = False
        self._incidentes_vistos = None

    def _avisar(self, texto):
        for canal in self.notificar:
            try:
                canal(texto)
            except (urllib.error.URLError, OSError):
                pass  # un canal caído no debe tumbar al vigía; se reintenta en la siguiente alerta

    def revisar(self):
        """Una ronda de revisión. Devuelve la lista de mensajes enviados."""
        enviados = []
        try:
            estado, salud = _peticion(self.url + "/api/salud", inseguro=self.inseguro)
            problema = None if estado == 200 else "; ".join((salud or {}).get("motivos", [])) or f"HTTP {estado}"
        except (urllib.error.URLError, OSError) as e:
            problema = f"sin respuesta ({getattr(e, 'reason', e)})"
        if problema:
            self._fallos += 1
            if self._fallos >= self.fallos_para_alertar and not self._alertado:
                enviados.append(f"⚠ Sentinela con problemas: {problema}. Revisar de inmediato.")
                self._alertado = True
        else:
            if self._alertado:
                enviados.append("✅ Sentinela restablecido.")
            self._fallos, self._alertado = 0, False
            enviados += self._incidentes_nuevos()
        for texto in enviados:
            self._avisar(texto)
        return enviados

    def _incidentes_nuevos(self):
        if not self.token:
            return []
        try:
            estado, lista = _peticion(self.url + "/api/incidentes", self.token, inseguro=self.inseguro)
        except (urllib.error.URLError, OSError):
            return []
        if estado != 200:
            return [f"⚠ El vigía no puede leer incidentes (HTTP {estado}): revisar el token."]
        ids = {i["id"] for i in lista}
        if self._incidentes_vistos is None:  # primera ronda: no repetir lo que ya existía
            self._incidentes_vistos = ids
            return []
        nuevos = sorted((i for i in lista if i["id"] not in self._incidentes_vistos), key=lambda i: i["id"])
        self._incidentes_vistos |= ids
        return [f"🚨 Incidente #{i['id']}: {i['tipo_texto']} en km {i['km']:.1f} ± {i['incertidumbre_km']:.1f} "
                f"(confianza {i['confianza']}). Acceso: {i['acceso']['nombre']}. "
                f"Brigada sugerida: {i['brigada_sugerida']['nombre']}, ETA {i['brigada_sugerida']['eta_min']} min."
                for i in nuevos]

    def correr(self, cada_s=30, salida=print):
        while True:
            for texto in self.revisar():
                salida(time.strftime("[%H:%M:%S] ") + texto)
            time.sleep(cada_s)
