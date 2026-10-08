import copy
import os
import tempfile
import unittest

from sentinela import config
from sentinela.deteccion import localizar_por_onda, localizar_por_perfil
from sentinela.ejecucion import analizar_csv, ejecutar_escenario, exportar_csv
from sentinela.incidentes import ErrorAccion

CFG = config.cargar_ducto()
BASE = config.cargar_escenario()


def escenario(*eventos, duracion=5400):
    esc = copy.deepcopy(BASE)
    esc["eventos"] = list(eventos)
    esc["duracion_s"] = duracion
    return esc


def toma(t, km, caudal, apertura=0, duracion=2400):
    return {"tipo": "toma", "t": t, "km": km, "caudal_m3h": caudal, "apertura_s": apertura, "duracion_s": duracion}


class TestLocalizacion(unittest.TestCase):
    def test_onda_exacta(self):
        v, x_real = 1.1, 47.3
        llegadas = [(k, 100 + abs(k - x_real) / v, 0.3) for k in (0, 20, 40, 60, 80)]
        x, t0, rmse = localizar_por_onda(llegadas, 120, v)
        self.assertAlmostEqual(x, x_real, delta=0.05)
        self.assertAlmostEqual(t0, 100, delta=0.05)
        self.assertLess(rmse, 0.01)

    def test_perfil_triangular(self):
        L, x_real = 120, 93.8
        deltas = [(k, 0.1 * (k / x_real if k <= x_real else (L - k) / (L - x_real))) for k in range(0, 121, 20)]
        x, D, _ = localizar_por_perfil(deltas, L)
        self.assertAlmostEqual(x, x_real, delta=0.1)
        self.assertAlmostEqual(D, 0.1, delta=0.005)


class TestEscenarios(unittest.TestCase):
    def test_sin_eventos_no_hay_falsas_alarmas(self):
        _, g = ejecutar_escenario(CFG, escenario())
        self.assertEqual(g.incidentes, {})

    def test_toma_abrupta_localizada_y_confirmada(self):
        _, g = ejecutar_escenario(CFG, escenario(toma(1200, 47.3, 18)))
        self.assertEqual(len(g.incidentes), 1)
        inc = g.incidentes[1]
        self.assertEqual(inc["tipo"], "toma_abrupta")
        self.assertAlmostEqual(inc["km"], 47.3, delta=1.0)
        self.assertEqual(inc["confianza"], "alta")
        self.assertLess(inc["t_deteccion"] - 1200, 300)
        self.assertAlmostEqual(inc["volumen_m3"], 18 * 2400 / 3600, delta=1.5)

    def test_toma_lenta_por_balance_y_perfil(self):
        _, g = ejecutar_escenario(CFG, escenario(toma(1200, 93.8, 10, apertura=900, duracion=3600)))
        self.assertEqual(len(g.incidentes), 1)
        inc = g.incidentes[1]
        self.assertEqual(inc["tipo"], "extraccion_gradual")
        self.assertAlmostEqual(inc["km"], 93.8, delta=CFG["deteccion"]["perfil_incertidumbre_km"])

    def test_maniobra_registrada_no_genera_incidente(self):
        ops = [{"tipo": "operacion", "t": 1500, "extremo": "entrada", "delta_bar": -6, "descripcion": "Paro de bomba", "registrada": True},
               {"tipo": "operacion", "t": 1800, "extremo": "entrada", "delta_bar": 6, "descripcion": "Arranque de bomba", "registrada": True}]
        _, g = ejecutar_escenario(CFG, escenario(*ops, duracion=3600))
        self.assertEqual(g.incidentes, {})
        self.assertTrue(any(e["tipo"] == "operativo" for e in g.eventos))

    def test_arranque_de_bomba_no_registrado_no_genera_incidente(self):
        # Regresión hallada con OPC-UA: sin bitácora, el re-empaque de la línea tras un
        # arranque de bomba desbalanceaba entrada/salida y se reportaba como extracción.
        ops = [{"tipo": "operacion", "t": t, "extremo": "entrada", "delta_bar": d, "descripcion": "x", "registrada": False}
               for t, d in ((1500, -6), (1800, 6), (2700, 6), (3000, -6))]
        _, g = ejecutar_escenario(CFG, escenario(*ops, duracion=4200))
        self.assertEqual(g.incidentes, {})

    def test_falla_de_sensor_no_despacha_brigada(self):
        falla = {"tipo": "falla_sensor", "t": 1200, "estacion": "E3", "delta_bar": -1.5, "duracion_s": 300}
        m, g = ejecutar_escenario(CFG, escenario(falla, duracion=2400))
        self.assertEqual(g.incidentes, {})
        self.assertEqual(m.estado_estaciones["E3"], "revisar")

    def test_escenario_demo_completo(self):
        _, g = ejecutar_escenario(CFG, BASE)
        tipos = sorted(i["tipo"] for i in g.incidentes.values())
        self.assertEqual(tipos, ["extraccion_gradual", "toma_abrupta"])


class TestGestion(unittest.TestCase):
    def test_flujo_de_estados(self):
        _, g = ejecutar_escenario(CFG, escenario(toma(1200, 47.3, 18), duracion=2400))
        g.aplicar_accion(1, "despachar", 2000)
        self.assertEqual(g.incidentes[1]["brigada"]["id"], "B-CENTRO")
        with self.assertRaises(ErrorAccion):
            g.aplicar_accion(1, "despachar", 2001)
        g.aplicar_accion(1, "en_sitio", 2500)
        g.aplicar_accion(1, "cerrar_confirmada", 3000, "Toma con válvula de 1/2 pulgada")
        self.assertEqual(g.incidentes[1]["estado"], "cerrado")
        self.assertEqual(g.kpis()["activos"], 0)


class TestCsv(unittest.TestCase):
    def test_ida_y_vuelta(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "lecturas.csv")
            bitacora = exportar_csv(CFG, escenario(toma(1200, 47.3, 18), duracion=2400), ruta)
            _, g = analizar_csv(CFG, ruta, bitacora)
        self.assertEqual(len(g.incidentes), 1)


if __name__ == "__main__":
    unittest.main()
