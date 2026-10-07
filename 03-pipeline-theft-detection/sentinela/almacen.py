"""Persistencia en SQLite de incidentes y eventos (auditable, sin servidor),
con respaldos en caliente y rotación."""
import glob
import json
import os
import sqlite3
import threading
import time


class Almacen:
    def __init__(self, ruta=":memory:"):
        self._con = sqlite3.connect(ruta, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._con:
            self._con.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidentes (
                    id INTEGER PRIMARY KEY,
                    estado TEXT NOT NULL,
                    datos TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eventos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    t REAL NOT NULL,
                    tipo TEXT NOT NULL,
                    severidad TEXT NOT NULL,
                    descripcion TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    clave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                );
                """
            )

    def guardar_incidente(self, inc):
        with self._lock, self._con:
            self._con.execute(
                "INSERT OR REPLACE INTO incidentes (id, estado, datos) VALUES (?, ?, ?)",
                (inc["id"], inc["estado"], json.dumps(inc, ensure_ascii=False)),
            )

    def guardar_evento(self, ev):
        with self._lock, self._con:
            self._con.execute(
                "INSERT INTO eventos (t, tipo, severidad, descripcion) VALUES (?, ?, ?, ?)",
                (ev["t"], ev["tipo"], ev["severidad"], ev["descripcion"]),
            )

    def incidentes(self):
        with self._lock:
            filas = self._con.execute("SELECT datos FROM incidentes ORDER BY id").fetchall()
        return [json.loads(f[0]) for f in filas]

    def eventos(self, limite=None):
        consulta = "SELECT t, tipo, severidad, descripcion FROM eventos ORDER BY id DESC"
        with self._lock:
            filas = self._con.execute(consulta + (" LIMIT ?" if limite else ""), (limite,) if limite else ()).fetchall()
        return [dict(zip(("t", "tipo", "severidad", "descripcion"), f)) for f in reversed(filas)]

    def leer_meta(self, clave):
        with self._lock:
            fila = self._con.execute("SELECT valor FROM meta WHERE clave = ?", (clave,)).fetchone()
        return json.loads(fila[0]) if fila else None

    def escribir_meta(self, clave, valor):
        with self._lock, self._con:
            self._con.execute("INSERT OR REPLACE INTO meta (clave, valor) VALUES (?, ?)", (clave, json.dumps(valor)))

    def respaldar(self, destino):
        """Copia consistente de la base mientras el sistema sigue escribiendo."""
        os.makedirs(os.path.dirname(os.path.abspath(destino)), exist_ok=True)
        copia = sqlite3.connect(destino)
        try:
            with self._lock:
                self._con.backup(copia)
        finally:
            copia.close()


class RespaldoPeriodico(threading.Thread):
    """Respalda la base cada `cada_s` segundos en `directorio` y conserva las últimas `conservar`."""

    def __init__(self, almacen, directorio, cada_s=3600, conservar=48, al_fallar=None):
        super().__init__(daemon=True)
        self.almacen = almacen
        self.directorio = directorio
        self.cada_s = cada_s
        self.conservar = conservar
        self.al_fallar = al_fallar
        self.ultimo = None  # {"ruta", "epoch", "ok", "error"}

    def respaldar_ahora(self):
        ruta = os.path.join(self.directorio, time.strftime("respaldo_%Y%m%d_%H%M%S.db"))
        try:
            self.almacen.respaldar(ruta)
            for viejo in sorted(glob.glob(os.path.join(self.directorio, "respaldo_*.db")))[:-self.conservar]:
                os.remove(viejo)
            self.ultimo = {"ruta": ruta, "epoch": time.time(), "ok": True, "error": None}
        except (OSError, sqlite3.Error) as e:
            self.ultimo = {"ruta": ruta, "epoch": time.time(), "ok": False, "error": str(e)}
            if self.al_fallar:
                self.al_fallar(f"Falló el respaldo de la base ({e}).")
        return self.ultimo

    def run(self):
        while True:
            time.sleep(self.cada_s)
            self.respaldar_ahora()
