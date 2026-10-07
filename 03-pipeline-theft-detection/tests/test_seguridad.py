import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from sentinela import config
from sentinela.almacen import Almacen, RespaldoPeriodico
from sentinela.auth import ErrorSesion, Sesiones, Usuarios, hash_contrasena, verificar_contrasena
from sentinela.ejecucion import crear_sistema
from sentinela.incidentes import GestorIncidentes
from sentinela.ingesta import Ingesta
from sentinela.monitor import Monitor
from sentinela.servidor import Aplicacion, crear_servidor
from sentinela.vigia import Vigia

CFG = config.cargar_ducto()
CLAVE = "clave-de-prueba-123"


class Reloj:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.usuarios = Usuarios(os.path.join(self.dir, "usuarios.json"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_hash(self):
        h = hash_contrasena(CLAVE)
        self.assertNotIn(CLAVE, h)
        self.assertTrue(verificar_contrasena(CLAVE, h))
        self.assertFalse(verificar_contrasena("otra", h))
        self.assertFalse(verificar_contrasena(CLAVE, "basura"))

    def test_alta_validaciones_y_baja(self):
        with self.assertRaises(ValueError):
            self.usuarios.agregar("ana", "corta", "operador")
        with self.assertRaises(ValueError):
            self.usuarios.agregar("ana", CLAVE, "jefe")
        with self.assertRaises(ValueError):
            self.usuarios.agregar("a b", CLAVE, "operador")
        self.usuarios.agregar("ana", CLAVE, "operador")
        self.assertEqual(self.usuarios.listar(), [("ana", "operador")])
        self.assertEqual(self.usuarios.autenticar("ana", CLAVE), "operador")
        self.assertIsNone(self.usuarios.autenticar("ana", "incorrecta-123"))
        self.assertIsNone(self.usuarios.autenticar("nadie", CLAVE))
        with open(self.usuarios.ruta, encoding="utf-8") as f:
            self.assertNotIn(CLAVE, f.read())
        self.usuarios.eliminar("ana")
        self.assertEqual(self.usuarios.listar(), [])

    def test_bloqueo_y_caducidad(self):
        self.usuarios.agregar("ana", CLAVE, "operador")
        reloj = Reloj()
        sesiones = Sesiones(self.usuarios, reloj)
        for _ in range(Sesiones.MAX_FALLOS):
            with self.assertRaises(ErrorSesion):
                sesiones.iniciar("ana", "incorrecta-123")
        with self.assertRaises(ErrorSesion) as e:
            sesiones.iniciar("ana", CLAVE)  # ni con la contraseña correcta mientras está bloqueado
        self.assertEqual(e.exception.codigo, 429)
        reloj.t += Sesiones.BLOQUEO_S + 1
        token, rol = sesiones.iniciar("ana", CLAVE)
        self.assertEqual(sesiones.validar(token)["rol"], "operador")
        reloj.t += Sesiones.DURACION_S + 1
        self.assertIsNone(sesiones.validar(token))


class Cliente:
    """Cliente HTTP mínimo que conserva la cookie de sesión."""

    def __init__(self, base, contexto=None):
        self.base, self.cookie, self.contexto = base, None, contexto

    def pedir(self, ruta, cuerpo=None, tipo="application/json", token=None):
        encabezados = {"Content-Type": tipo}
        if self.cookie:
            encabezados["Cookie"] = self.cookie
        if token:
            encabezados["Authorization"] = f"Bearer {token}"
        datos = None if cuerpo is None else (cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode())
        req = urllib.request.Request(self.base + ruta, data=datos, headers=encabezados, method="POST" if datos else "GET")
        try:
            with urllib.request.urlopen(req, context=self.contexto) as r:
                estado, enc, cuerpo_r = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            estado, enc, cuerpo_r = e.code, e.headers, e.read()
        if enc.get("Set-Cookie"):
            self.cookie = enc["Set-Cookie"].split(";")[0]
        return estado, json.loads(cuerpo_r or b"null"), enc

    def entrar(self, usuario):
        return self.pedir("/api/sesion", {"usuario": usuario, "contrasena": CLAVE})


class TestServidorSeguro(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        usuarios = Usuarios(os.path.join(cls.dir, "usuarios.json"))
        for nombre, rol in (("lector", "lectura"), ("oper", "operador"), ("super", "supervisor")):
            usuarios.agregar(nombre, CLAVE, rol)
        monitor, gestor = crear_sistema(CFG)
        gestor.crear(100, "toma_abrupta", 47.3, 0.5, 90, "prueba", "media")
        cls.app = Aplicacion(CFG, monitor, gestor, ingesta=Ingesta(CFG, monitor), token="maquina-123",
                             sesiones=Sesiones(usuarios))
        cls.srv = crear_servidor(cls.app, 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.dir)

    def test_sin_sesion_no_hay_datos(self):
        c = Cliente(self.base)
        for ruta in ("/api/estado", "/api/incidentes", "/api/eventos", "/api/calidad"):
            self.assertEqual(c.pedir(ruta)[0], 401, ruta)
        estado, salud, enc = c.pedir("/api/salud")  # la salud es pública pero sin datos sensibles
        self.assertIn(estado, (200, 503))
        self.assertNotIn("incidentes", json.dumps(salud))
        self.assertEqual(enc["X-Frame-Options"], "DENY")

    def test_contrasena_incorrecta(self):
        estado, r, _ = Cliente(self.base).pedir("/api/sesion", {"usuario": "oper", "contrasena": "mala-mala-1"})
        self.assertEqual(estado, 401)

    def test_cookie_segura(self):
        c = Cliente(self.base)
        estado, _, enc = c.entrar("lector")
        self.assertEqual(estado, 200)
        self.assertIn("HttpOnly", enc["Set-Cookie"])
        self.assertIn("SameSite=Strict", enc["Set-Cookie"])

    def test_permisos_por_rol(self):
        lector, oper, sup = Cliente(self.base), Cliente(self.base), Cliente(self.base)
        lector.entrar("lector")
        oper.entrar("oper")
        sup.entrar("super")
        self.assertEqual(lector.pedir("/api/incidentes")[0], 200)
        self.assertEqual(lector.pedir("/api/incidentes/1/despachar", {})[0], 403)
        self.assertEqual(oper.pedir("/api/incidentes/1/despachar", {})[0], 200)
        self.assertEqual(oper.pedir("/api/incidentes/1/cerrar_falsa", {})[0], 403)
        estado, inc, _ = sup.pedir("/api/incidentes/1/cerrar_falsa", {"comentario": "verificado"})
        self.assertEqual(estado, 200)
        self.assertIn("(por super)", inc["notas"][-1]["texto"])  # auditoría: quién lo hizo
        sup.pedir("/api/sesion/cerrar", {})
        self.assertEqual(sup.pedir("/api/incidentes")[0], 401)

    def test_csrf_requiere_json(self):
        c = Cliente(self.base)
        c.entrar("super")
        estado, _, _ = c.pedir("/api/incidentes/1/despachar", b"comentario=x", tipo="application/x-www-form-urlencoded")
        self.assertEqual(estado, 415)

    def test_token_de_maquina_lee_pero_no_actua(self):
        c = Cliente(self.base)
        self.assertEqual(c.pedir("/api/incidentes", token="maquina-123")[0], 200)
        self.assertEqual(c.pedir("/api/incidentes/1/despachar", {}, token="maquina-123")[0], 403)
        self.assertEqual(c.pedir("/api/incidentes", token="falso")[0], 401)


@unittest.skipUnless(shutil.which("openssl"), "requiere openssl para generar un certificado de prueba")
class TestHttps(unittest.TestCase):
    def test_tls(self):
        with tempfile.TemporaryDirectory() as d:
            cert, llave = os.path.join(d, "cert.pem"), os.path.join(d, "llave.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", llave, "-out", cert,
                            "-days", "1", "-subj", "/CN=127.0.0.1"], check=True, capture_output=True)
            monitor, gestor = crear_sistema(CFG)
            app = Aplicacion(CFG, monitor, gestor, inicio_epoch=0)
            srv = crear_servidor(app, 0, certificado=cert, llave=llave)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                contexto = ssl.create_default_context(cafile=cert)
                contexto.check_hostname = False
                estado, salud, enc = Cliente(f"https://127.0.0.1:{srv.server_address[1]}", contexto).pedir("/api/salud")
                self.assertEqual(estado, 200)
                self.assertIn("Strict-Transport-Security", enc)
                with self.assertRaises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):  # sin TLS no responde
                    urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/salud", timeout=3)
            finally:
                srv.shutdown()


class TestRespaldosYReinicio(unittest.TestCase):
    def test_reinicio_continua_el_turno(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "turno.db")
            g1 = GestorIncidentes(CFG, Almacen(ruta))
            g1.crear(100, "toma_abrupta", 47.3, 0.5, 90, "prueba", "media")
            g1.aplicar_accion(1, "despachar", 120, usuario="oper")
            g1.almacen.escribir_meta("inicio_epoch", 1_800_000_000)
            g1.almacen._con.close()
            g2 = GestorIncidentes(CFG, Almacen(ruta))  # "reinicio"
            self.assertEqual(g2.incidentes[1]["estado"], "despachado")
            self.assertEqual(g2.crear(200, "toma_abrupta", 90.0, 0.5, 190, "prueba", "media")["id"], 2)
            self.assertEqual(g2.almacen.leer_meta("inicio_epoch"), 1_800_000_000)
            self.assertTrue(any(e["tipo"] == "incidente" for e in g2.eventos))
            g2.almacen._con.close()

    def test_respaldo_en_caliente_y_rotacion(self):
        with tempfile.TemporaryDirectory() as d:
            almacen = Almacen(os.path.join(d, "turno.db"))
            gestor = GestorIncidentes(CFG, almacen)
            gestor.crear(100, "toma_abrupta", 47.3, 0.5, 90, "prueba", "media")
            r = RespaldoPeriodico(almacen, os.path.join(d, "respaldos"), conservar=2)
            rutas = []
            for i in range(3):
                destino = os.path.join(d, "respaldos", f"respaldo_2026010{i}_000000.db")
                almacen.respaldar(destino)
                rutas.append(destino)
            self.assertTrue(r.respaldar_ahora()["ok"])
            self.assertEqual(len(os.listdir(os.path.join(d, "respaldos"))), 2)
            copia = Almacen(r.ultimo["ruta"])
            self.assertEqual(copia.incidentes()[0]["km"], 47.3)
            copia._con.close()
            almacen._con.close()


class TestVigia(unittest.TestCase):
    def test_servidor_caido_avisa_una_vez(self):
        avisos = []
        v = Vigia(f"http://127.0.0.1:{puerto_libre()}", [avisos.append], fallos_para_alertar=2)
        v.revisar()
        self.assertEqual(avisos, [])
        v.revisar()
        v.revisar()
        self.assertEqual(len(avisos), 1)
        self.assertIn("sin respuesta", avisos[0])

    def test_incidente_nuevo_y_recuperacion(self):
        monitor, gestor = crear_sistema(CFG)
        app = Aplicacion(CFG, monitor, gestor, inicio_epoch=0, token="maquina-123")
        srv = crear_servidor(app, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            avisos = []
            v = Vigia(f"http://127.0.0.1:{srv.server_address[1]}", [avisos.append], token="maquina-123")
            v.revisar()  # línea base
            with monitor.lock:
                gestor.crear(100, "toma_abrupta", 47.3, 0.5, 90, "prueba", "media")
            v.revisar()
            self.assertEqual(len(avisos), 1)
            self.assertIn("km 47.3", avisos[0])
            v.revisar()
            self.assertEqual(len(avisos), 1)  # no se repite
        finally:
            srv.shutdown()


class TestCalibracionRelativa(unittest.TestCase):
    def test_monitor_con_t_grande_calibra(self):
        monitor = Monitor(CFG, crear_sistema(CFG)[1])
        for t in range(10**6, 10**6 + 700):
            monitor.procesar(t, {e["id"]: 50.0 for e in CFG["estaciones"]}, 900.0, 899.0)
        self.assertFalse(monitor.balance.calibrando)
        self.assertAlmostEqual(monitor.balance.sesgo, 1.0)


if __name__ == "__main__":
    unittest.main()
