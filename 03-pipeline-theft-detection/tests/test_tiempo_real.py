import copy
import json
import random
import threading
import unittest
import urllib.error
import urllib.request

from sentinela import config
from sentinela.ejecucion import crear_sistema
from sentinela.ingesta import Ingesta
from sentinela.servidor import Aplicacion, crear_servidor
from sentinela.simulador import Simulador

CFG = config.cargar_ducto()
T0 = 1_800_000_000  # epoch arbitrario de las pruebas


class Reloj:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def sistema(verificar_reloj=False, **kw):
    monitor, gestor = crear_sistema(CFG)
    reloj = Reloj(T0)
    return Ingesta(CFG, monitor, reloj=reloj, verificar_reloj=verificar_reloj, **kw), monitor, gestor, reloj


def foto(ingesta, ts, presion=50.0, q=900.0, omitir=()):
    for e in ingesta.etiquetas:
        if e not in omitir:
            ingesta.recibir(e, ts, q if e.startswith("q_") else presion)


class TestIngesta(unittest.TestCase):
    def test_validacion(self):
        ing, *_ = sistema(verificar_reloj=True)
        self.assertEqual(ing.recibir("E9", T0, 50), "etiqueta_desconocida")
        self.assertEqual(ing.recibir("E2", T0, 999), "fuera_de_rango")
        self.assertEqual(ing.recibir("E2", T0, "NaN"), "valor_invalido")
        self.assertEqual(ing.recibir("E2", T0, None), "valor_invalido")
        self.assertEqual(ing.recibir("E2", T0, True), "valor_invalido")
        self.assertEqual(ing.recibir("E2", T0 + 60, 50), "marca_tiempo_futura")
        self.assertIsNone(ing.recibir("E2", T0, "50.5"))

    def test_desorden_dentro_de_la_latencia(self):
        ing, monitor, _, reloj = sistema(latencia_s=3)
        segundos = list(range(20))
        random.Random(3).shuffle(segundos[:10])  # los primeros 10 llegan desordenados
        for s in segundos:
            reloj.t = T0 + s
            foto(ing, T0 + s + 0.5, presion=50 + s)
        ing.avanzar()
        tiempos = [d[0] for d in monitor.series]
        self.assertEqual(tiempos, list(range(len(tiempos))))
        self.assertEqual(monitor.series[5][1]["E0"], 55)
        self.assertEqual(ing.segundos_omitidos, 0)

    def test_tardia_rechazada(self):
        ing, *_ = sistema(latencia_s=2)
        for s in range(10):
            foto(ing, T0 + s)
        ing.avanzar()
        self.assertEqual(ing.recibir("E0", T0 + 1, 50), "tardia")

    def test_hueco_corto_se_rellena(self):
        ing, monitor, _, _ = sistema(max_hueco_s=10)
        for s in range(30):
            foto(ing, T0 + s, omitir=("E3",) if 10 <= s < 15 else ())
        ing.avanzar()
        self.assertEqual(ing.segundos_omitidos, 0)
        self.assertEqual(ing.rellenos["E3"], 5)

    def test_hueco_largo_suspende_y_reinicia(self):
        ing, monitor, gestor, _ = sistema(max_hueco_s=5)
        for s in range(60):
            foto(ing, T0 + s, omitir=("E3",) if 10 <= s < 40 else ())
        ing.avanzar()
        self.assertGreater(ing.segundos_omitidos, 20)
        textos = [e["descripcion"] for e in gestor.eventos]
        self.assertTrue(any("E3 dejó de reportar" in t for t in textos))
        self.assertTrue(any("Se reanudó el flujo" in t for t in textos))

    def test_caida_total_del_enlace(self):
        ing, monitor, gestor, reloj = sistema(max_hueco_s=5)
        for s in range(20):
            foto(ing, T0 + s)
        reloj.t = T0 + 20
        ing.avanzar()
        reloj.t = T0 + 3600  # una hora sin datos
        ing.avanzar()
        self.assertEqual(ing.enlace, "caido")
        for s in range(3600, 3620):
            foto(ing, T0 + s)
        ing.avanzar()
        self.assertEqual(ing.enlace, "ok")
        self.assertGreater(monitor.t, 3600)


class TestTiempoRealDeExtremoAExtremo(unittest.TestCase):
    def test_toma_detectada_con_datos_imperfectos(self):
        esc = copy.deepcopy(config.cargar_escenario())
        esc["duracion_s"] = 2400
        esc["eventos"] = [{"tipo": "toma", "t": 1200, "km": 47.3, "caudal_m3h": 18, "apertura_s": 0, "duracion_s": 1200}]
        sim = Simulador(CFG, esc)
        ing, monitor, gestor, reloj = sistema(latencia_s=3, max_hueco_s=10)
        rng = random.Random(5)
        retenidas = []
        for t in range(esc["duracion_s"]):
            reloj.t = T0 + t + 1
            l = sim.leer(t)
            lote = []
            for e, v in {**l.presiones, "q_entrada": l.q_entrada, "q_salida": l.q_salida}.items():
                if rng.random() < 0.02:
                    continue  # 2 % de lecturas perdidas
                lectura = (e, T0 + t + rng.uniform(0, 0.9), v)
                (retenidas if rng.random() < 0.05 else lote).append(lectura)
            if t % 2:
                lote, retenidas = lote + retenidas, []  # las retenidas llegan 1 s tarde
            rng.shuffle(lote)
            for lectura in lote:
                ing.recibir(*lectura)
            ing.avanzar()
        self.assertEqual(len(gestor.incidentes), 1)
        inc = gestor.incidentes[1]
        self.assertEqual(inc["tipo"], "toma_abrupta")
        self.assertAlmostEqual(inc["km"], 47.3, delta=1.5)
        self.assertEqual(inc["confianza"], "alta")


class TestApiHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        monitor, gestor = crear_sistema(CFG)
        cls.ingesta = Ingesta(CFG, monitor, verificar_reloj=False)
        app = Aplicacion(CFG, monitor, gestor, ingesta=cls.ingesta, token="secreto")
        cls.srv = crear_servidor(app, 0)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, ruta, cuerpo, token="secreto"):
        req = urllib.request.Request(self.url + ruta, data=json.dumps(cuerpo).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_token_obligatorio(self):
        estado, _ = self.post("/api/lecturas", {"lecturas": []}, token="malo")
        self.assertEqual(estado, 401)

    def test_lecturas_por_etiqueta_y_por_foto(self):
        estado, r = self.post("/api/lecturas", {"lecturas": [
            {"etiqueta": "E0", "ts": T0, "valor": 80.0},
            {"etiqueta": "E2", "ts": T0, "valor": 999},
            {"etiqueta": "X", "ts": T0, "valor": 1},
        ]})
        self.assertEqual(estado, 200)
        self.assertEqual(r["aceptadas"], 1)
        self.assertEqual([x["motivo"] for x in r["rechazadas"]], ["fuera_de_rango", "etiqueta_desconocida"])
        estado, r = self.post("/api/lecturas", {"ts": T0 + 1, "presiones": {"E0": 80, "E1": 69}, "q_entrada": 900, "q_salida": 899})
        self.assertEqual((estado, r["aceptadas"]), (200, 4))

    def test_bitacora_y_errores(self):
        self.assertEqual(self.post("/api/bitacora", {"ts": T0, "descripcion": "Paro de bomba"})[0], 200)
        self.assertEqual(self.post("/api/bitacora", {"ts": T0, "descripcion": ""})[0], 400)
        self.assertEqual(self.post("/api/lecturas", {"otra": 1})[0], 400)

    def test_calidad(self):
        with urllib.request.urlopen(self.url + "/api/calidad") as r:
            c = json.loads(r.read())
        self.assertIn("E0", c["etiquetas"])


if __name__ == "__main__":
    unittest.main()
