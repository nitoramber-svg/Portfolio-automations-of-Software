"""Usuarios, roles y sesiones del tablero.

- Contraseñas con PBKDF2-SHA256 (200 000 iteraciones, sal por usuario); nunca en claro.
- Roles jerárquicos: lectura < operador < supervisor < admin.
- Sesiones con token aleatorio de 256 bits y caducidad; bloqueo tras intentos fallidos.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

ROLES = ("lectura", "operador", "supervisor", "admin")
NIVEL = {r: i for i, r in enumerate(ROLES)}
ITERACIONES = 200_000
NOMBRE_VALIDO = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_CONTRASENA = 10


class ErrorSesion(Exception):
    def __init__(self, mensaje, codigo=401):
        super().__init__(mensaje)
        self.codigo = codigo


def hash_contrasena(contrasena, sal=None, iteraciones=ITERACIONES):
    sal = sal or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", contrasena.encode("utf-8"), sal, iteraciones)
    return f"pbkdf2_sha256${iteraciones}${sal.hex()}${h.hex()}"


def verificar_contrasena(contrasena, guardado):
    try:
        _, iteraciones, sal, h = guardado.split("$")
        calculado = hashlib.pbkdf2_hmac("sha256", contrasena.encode("utf-8"), bytes.fromhex(sal), int(iteraciones))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calculado.hex(), h)


def tiene_rol(rol, minimo):
    return rol in NIVEL and NIVEL[rol] >= NIVEL[minimo]


class Usuarios:
    """Archivo JSON {usuario: {"rol": ..., "hash": ...}}. Se relee si cambia en disco,
    así que dar de alta o baja usuarios no requiere reiniciar el servidor."""

    _HASH_FICTICIO = hash_contrasena("contraseña-ficticia")

    def __init__(self, ruta):
        self.ruta = ruta
        self._datos = {}
        self._mtime = None
        self._lock = threading.Lock()

    def _cargar(self):
        try:
            mtime = os.path.getmtime(self.ruta)
        except OSError:
            self._datos, self._mtime = {}, None
            return
        if mtime != self._mtime:
            with open(self.ruta, encoding="utf-8") as f:
                self._datos = json.load(f)
            self._mtime = mtime

    def _guardar(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.ruta)), exist_ok=True)
        temporal = self.ruta + ".tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(self._datos, f, ensure_ascii=False, indent=2)
        os.replace(temporal, self.ruta)  # escritura atómica
        self._mtime = os.path.getmtime(self.ruta)

    def agregar(self, usuario, contrasena, rol):
        if not NOMBRE_VALIDO.match(usuario or ""):
            raise ValueError("El usuario debe tener 3–32 caracteres: letras, números, punto, guion o guion bajo.")
        if rol not in NIVEL:
            raise ValueError(f"Rol inválido. Opciones: {', '.join(ROLES)}.")
        if len(contrasena or "") < MIN_CONTRASENA:
            raise ValueError(f"La contraseña debe tener al menos {MIN_CONTRASENA} caracteres.")
        with self._lock:
            self._cargar()
            self._datos[usuario] = {"rol": rol, "hash": hash_contrasena(contrasena)}
            self._guardar()

    def eliminar(self, usuario):
        with self._lock:
            self._cargar()
            if self._datos.pop(usuario, None) is None:
                raise ValueError(f"No existe el usuario {usuario}.")
            self._guardar()

    def listar(self):
        with self._lock:
            self._cargar()
            return sorted((u, d["rol"]) for u, d in self._datos.items())

    def autenticar(self, usuario, contrasena):
        """Devuelve el rol o None. Tarda lo mismo exista o no el usuario."""
        with self._lock:
            self._cargar()
            datos = self._datos.get(usuario)
        if datos is None:
            verificar_contrasena(contrasena, self._HASH_FICTICIO)
            return None
        return datos["rol"] if verificar_contrasena(contrasena, datos["hash"]) else None


class Sesiones:
    DURACION_S = 12 * 3600
    MAX_FALLOS = 5
    BLOQUEO_S = 300

    def __init__(self, usuarios, reloj=time.time):
        self.usuarios = usuarios
        self.reloj = reloj
        self._sesiones = {}  # token -> {"usuario", "rol", "expira"}
        self._fallos = {}    # usuario -> [conteo, bloqueado_hasta]
        self._lock = threading.Lock()

    def iniciar(self, usuario, contrasena):
        usuario = str(usuario or "")
        ahora = self.reloj()
        with self._lock:
            conteo, hasta = self._fallos.get(usuario, [0, 0])
            if hasta > ahora:
                raise ErrorSesion(f"Usuario bloqueado por intentos fallidos; espere {int(hasta - ahora)} s.", 429)
        rol = self.usuarios.autenticar(usuario, str(contrasena or ""))
        with self._lock:
            if rol is None:
                conteo += 1
                self._fallos[usuario] = [0, ahora + self.BLOQUEO_S] if conteo >= self.MAX_FALLOS else [conteo, 0]
                raise ErrorSesion("Usuario o contraseña incorrectos.")
            self._fallos.pop(usuario, None)
            token = secrets.token_urlsafe(32)
            self._sesiones[token] = {"usuario": usuario, "rol": rol, "expira": ahora + self.DURACION_S}
            # Limpieza de sesiones vencidas
            for t in [t for t, s in self._sesiones.items() if s["expira"] <= ahora]:
                del self._sesiones[t]
            return token, rol

    def validar(self, token):
        if not token:
            return None
        with self._lock:
            s = self._sesiones.get(token)
            if s is None or s["expira"] <= self.reloj():
                self._sesiones.pop(token, None)
                return None
            return {"usuario": s["usuario"], "rol": s["rol"]}

    def cerrar(self, token):
        with self._lock:
            self._sesiones.pop(token, None)
