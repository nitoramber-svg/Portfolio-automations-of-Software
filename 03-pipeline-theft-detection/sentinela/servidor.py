"""API HTTP + tablero web del centro de monitoreo (solo biblioteca estándar).

Seguridad:
- Personas: sesión con usuario/contraseña (cookie HttpOnly, SameSite=Strict, Secure con HTTPS)
  y permisos por rol. Sin archivo de usuarios el tablero queda abierto (solo uso local).
- Máquinas (pasarela SCADA, vigía): token Bearer; puede enviar lecturas y consultar en modo lectura.
- HTTPS opcional con certificado propio; encabezados que impiden incrustar o reinterpretar la página.
"""
import hmac
import json
import os
import re
import ssl
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .auth import ErrorSesion, tiene_rol
from .incidentes import ErrorAccion

INDEX = os.path.join(os.path.dirname(__file__), "web", "index.html")
RUTA_ACCION = re.compile(r"^/api/incidentes/(\d+)/(\w+)$")
MAX_CUERPO = 2 * 1024 * 1024
COOKIE = "sentinela_sesion"

# Rol mínimo para cada acción. Ver es "lectura".
PERMISOS = {
    "despachar": "operador",
    "en_sitio": "operador",
    "cerrar_confirmada": "supervisor",
    "cerrar_falsa": "supervisor",
    "simulacion": "supervisor",
}

ENCABEZADOS_SEGURIDAD = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
}


@dataclass
class Aplicacion:
    cfg: dict
    monitor: object
    gestor: object
    ejecutor: object = None      # simulación en vivo (modo demo)
    ingesta: object = None       # datos reales (modo servidor)
    inicio_epoch: float = None
    token: str = None            # token de máquina (pasarela SCADA, vigía)
    sesiones: object = None      # None = tablero sin inicio de sesión (solo local)
    respaldos: object = None
    https: bool = False
    extra: dict = field(default_factory=dict)

    def inicio(self):
        return self.ingesta.inicio if self.ingesta else self.inicio_epoch

    def salud(self):
        """Estado para monitoreo externo: 'ok' o 'degradado' con motivos. Sin datos sensibles."""
        motivos = []
        datos = {"version": __version__, "modo": "tiempo_real" if self.ingesta else "simulacion"}
        if self.ingesta:
            c = self.ingesta.calidad()
            datos["enlace"] = c["enlace"]
            datos["segundos_sin_datos"] = c["segundos_sin_datos"]
            if c["enlace"] != "ok":
                motivos.append(f"enlace SCADA {c['enlace']}")
            sin_datos = [e for e, d in c["etiquetas"].items() if d["estado"] == "sin_datos"]
            if sin_datos and c["enlace"] == "ok":
                motivos.append(f"etiquetas sin datos: {', '.join(sin_datos)}")
        if self.respaldos and self.respaldos.ultimo and not self.respaldos.ultimo["ok"]:
            motivos.append("último respaldo fallido")
        datos["respaldo"] = None if not (self.respaldos and self.respaldos.ultimo) else \
            {"epoch": self.respaldos.ultimo["epoch"], "ok": self.respaldos.ultimo["ok"]}
        return {"estado": "degradado" if motivos else "ok", "motivos": motivos, **datos}


def _lecturas_del_cuerpo(cuerpo):
    """Acepta lecturas por etiqueta o una foto completa de un instante."""
    if "lecturas" in cuerpo:
        if not isinstance(cuerpo["lecturas"], list):
            raise ValueError("'lecturas' debe ser una lista")
        return [(l.get("etiqueta"), l.get("ts"), l.get("valor")) if isinstance(l, dict) else (None, None, None)
                for l in cuerpo["lecturas"]]
    if "ts" in cuerpo:
        ts, presiones = cuerpo["ts"], cuerpo.get("presiones") or {}
        if not isinstance(presiones, dict):
            raise ValueError("'presiones' debe ser un objeto {estación: bar}")
        salida = [(e, ts, v) for e, v in presiones.items()]
        salida += [(e, ts, cuerpo[e]) for e in ("q_entrada", "q_salida") if e in cuerpo]
        return salida
    raise ValueError("Se esperaba {'lecturas': [...]} o {'ts': ..., 'presiones': {...}, 'q_entrada': ..., 'q_salida': ...}")


def crear_servidor(app, puerto, host="127.0.0.1", certificado=None, llave=None):

    class Manejador(BaseHTTPRequestHandler):
        server_version = "Sentinela"
        sys_version = ""

        def log_message(self, *args):
            pass

        # ------------------------------------------------------------ utilidades
        def _enviar(self, estado, cuerpo, tipo, encabezados=()):
            self.send_response(estado)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            for k, v in ENCABEZADOS_SEGURIDAD.items():
                self.send_header(k, v)
            if app.https:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in encabezados:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(cuerpo)

        def _json(self, datos, estado=200, encabezados=()):
            self._enviar(estado, json.dumps(datos, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8", encabezados)

        def _cuerpo(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_CUERPO:
                raise OverflowError
            datos = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(datos, dict):
                raise ValueError
            return datos

        def _token_maquina(self):
            recibido = self.headers.get("Authorization", "")
            if not (app.token and recibido.startswith("Bearer ")):
                return False
            return hmac.compare_digest(recibido[7:].strip().encode(), app.token.encode())

        def _token_cookie(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c[COOKIE].value if COOKIE in c else None

        def _quien(self):
            """Identidad de quien llama: dict con usuario y rol, o None."""
            if app.sesiones is None:
                return {"usuario": None, "rol": "admin", "via": "local"}
            if self._token_maquina():
                return {"usuario": "maquina", "rol": "lectura", "via": "token"}
            s = app.sesiones.validar(self._token_cookie())
            return {**s, "via": "sesion"} if s else None

        def _evento(self, tipo, texto, severidad="info"):
            with app.monitor.lock:
                app.gestor.registrar_evento(app.monitor.t, tipo, texto, severidad)

        def _simulacion(self):
            e = app.ejecutor
            if not e:
                return None
            return {"velocidad": e.velocidad, "pausado": e.pausado, "terminado": e.terminado, "duracion": e.sim.duracion}

        # ------------------------------------------------------------------ GET
        def do_GET(self):
            ruta = self.path.split("?")[0]
            if ruta in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    return self._enviar(200, f.read(), "text/html; charset=utf-8")
            if ruta == "/api/salud":
                s = app.salud()
                return self._json(s, 200 if s["estado"] == "ok" else 503)
            quien = self._quien()
            if ruta == "/api/sesion":
                if not quien:
                    return self._json({"error": "Sin sesión"}, 401)
                return self._json({"usuario": quien["usuario"], "rol": quien["rol"],
                                   "autenticacion": app.sesiones is not None, "permisos": PERMISOS})
            if not quien:
                return self._json({"error": "Inicie sesión"}, 401)
            if ruta == "/api/calidad":  # fuera del candado del monitor: la ingesta lo toma en orden inverso
                return self._json(app.ingesta.calidad() if app.ingesta else None)
            with app.monitor.lock:
                if ruta == "/api/config":
                    c = app.cfg
                    return self._json({"ducto": c["ducto"], "estaciones": c["estaciones"], "puntos_acceso": c["puntos_acceso"],
                                       "brigadas": c["brigadas"], "inicio_epoch": app.inicio(),
                                       "modo": "tiempo_real" if app.ingesta else "simulacion"})
                if ruta == "/api/estado":
                    return self._json({**app.monitor.estado(), "inicio_epoch": app.inicio(), "simulacion": self._simulacion()})
                if ruta == "/api/series":
                    return self._json(app.monitor.series_json())
                if ruta == "/api/incidentes":
                    return self._json(app.gestor.lista())
                if ruta == "/api/eventos":
                    return self._json(list(reversed(app.gestor.eventos)))
            self._json({"error": "No encontrado"}, 404)

        # ----------------------------------------------------------------- POST
        def do_POST(self):
            # API solo JSON. Además es la defensa CSRF: un formulario de otro sitio no puede
            # enviar application/json sin pasar por una verificación CORS que este servidor no concede.
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "Se requiere Content-Type: application/json"}, 415)
            try:
                cuerpo = self._cuerpo()
            except OverflowError:
                return self._json({"error": f"Cuerpo mayor a {MAX_CUERPO // 1024 // 1024} MB"}, 413)
            except ValueError:
                return self._json({"error": "JSON inválido"}, 400)

            if self.path == "/api/sesion":
                return self._iniciar_sesion(cuerpo)
            if self.path == "/api/sesion/cerrar":
                if app.sesiones:
                    app.sesiones.cerrar(self._token_cookie())
                return self._json({"ok": True}, encabezados=[("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0")])

            if self.path in ("/api/lecturas", "/api/bitacora"):
                return self._ingresar(cuerpo)

            quien = self._quien()
            if not quien:
                return self._json({"error": "Inicie sesión"}, 401)
            m = RUTA_ACCION.match(self.path)
            if m:
                accion = m.group(2)
                minimo = PERMISOS.get(accion)
                if minimo is None:
                    return self._json({"error": f"Acción desconocida: {accion}"}, 404)
                if quien["via"] == "token" or not tiene_rol(quien["rol"], minimo):
                    return self._json({"error": f"Su rol ({quien['rol']}) no permite '{accion}'; requiere {minimo}."}, 403)
                with app.monitor.lock:
                    try:
                        inc = app.gestor.aplicar_accion(int(m.group(1)), accion, app.monitor.t,
                                                        str(cuerpo.get("comentario", "")), quien["usuario"])
                    except ErrorAccion as e:
                        return self._json({"error": str(e)}, 409)
                return self._json(inc)
            if self.path == "/api/simulacion" and app.ejecutor:
                if not tiene_rol(quien["rol"], PERMISOS["simulacion"]):
                    return self._json({"error": "Requiere rol supervisor"}, 403)
                if "velocidad" in cuerpo:
                    app.ejecutor.velocidad = max(1, min(200, int(cuerpo["velocidad"])))
                if "pausado" in cuerpo:
                    app.ejecutor.pausado = bool(cuerpo["pausado"])
                return self._json(self._simulacion())
            self._json({"error": "No encontrado"}, 404)

        def _iniciar_sesion(self, cuerpo):
            if app.sesiones is None:
                return self._json({"usuario": None, "rol": "admin", "autenticacion": False})
            usuario = str(cuerpo.get("usuario", ""))[:64]
            try:
                token, rol = app.sesiones.iniciar(usuario, cuerpo.get("contrasena"))
            except ErrorSesion as e:
                self._evento("seguridad", f"Inicio de sesión fallido para «{usuario}» desde {self.client_address[0]}: {e}",
                             "media")
                return self._json({"error": str(e)}, e.codigo)
            self._evento("seguridad", f"Inicio de sesión de {usuario} ({rol}) desde {self.client_address[0]}.")
            atributos = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={app.sesiones.DURACION_S}"
            if app.https:
                atributos += "; Secure"
            return self._json({"usuario": usuario, "rol": rol, "autenticacion": True, "permisos": PERMISOS},
                              encabezados=[("Set-Cookie", atributos)])

        def _ingresar(self, cuerpo):
            if not app.ingesta:
                return self._json({"error": "Este servidor está en modo simulación; use `python -m sentinela servidor`."}, 409)
            if app.token and not self._token_maquina():
                return self._json({"error": "Token inválido o ausente (Authorization: Bearer <token>)"}, 401)
            if self.path == "/api/bitacora":
                motivo = app.ingesta.recibir_bitacora(cuerpo.get("ts"), cuerpo.get("descripcion"))
                return self._json({"error": motivo}, 400) if motivo else self._json({"aceptada": True})
            try:
                lecturas = _lecturas_del_cuerpo(cuerpo)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            rechazadas = []
            for i, (etiqueta, ts, valor) in enumerate(lecturas):
                motivo = app.ingesta.recibir(etiqueta, ts, valor)
                if motivo:
                    rechazadas.append({"indice": i, "etiqueta": etiqueta, "motivo": motivo})
            return self._json({"aceptadas": len(lecturas) - len(rechazadas), "rechazadas": rechazadas})

    servidor = ThreadingHTTPServer((host, puerto), Manejador)
    if certificado:
        contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        contexto.minimum_version = ssl.TLSVersion.TLSv1_2
        contexto.load_cert_chain(certificado, llave)
        # El saludo TLS se hace en el hilo de cada conexión, no en el que acepta:
        # un cliente lento no puede bloquear al servidor.
        servidor.socket = contexto.wrap_socket(servidor.socket, server_side=True, do_handshake_on_connect=False)
        app.https = True
    return servidor
