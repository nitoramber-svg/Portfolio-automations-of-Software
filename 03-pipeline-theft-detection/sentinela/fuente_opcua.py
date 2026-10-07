"""Cliente OPC-UA: se suscribe a las etiquetas del SCADA y alimenta la ingesta.

Requiere la dependencia opcional `asyncua` (pip install asyncua). El mapeo de
etiquetas a nodos se define en un JSON como `datos/mapa_opcua_ejemplo.json`.
Se reconecta solo si el servidor OPC-UA se cae.

Seguridad (IEC 62443): la conexión es de solo lectura (suscripciones; jamás escribe en el
SCADA) y puede ir firmada y cifrada con certificados. En el mapa:

    "seguridad": {"politica": "Basic256Sha256", "modo": "SignAndEncrypt",
                  "certificado": "certs/cliente.der", "llave": "certs/cliente_llave.pem",
                  "certificado_servidor": "certs/servidor.der"},
    "usuario_env": "SENTINELA_OPCUA_USUARIO", "contrasena_env": "SENTINELA_OPCUA_CONTRASENA"

Las credenciales se leen de variables de entorno, nunca del archivo.
"""
import asyncio
import json
import os
import threading
from datetime import timezone


class FuenteOPCUA(threading.Thread):
    REINTENTO_S = 5

    def __init__(self, ruta_mapa, ingesta):
        super().__init__(daemon=True)
        with open(ruta_mapa, encoding="utf-8") as f:
            self.mapa = json.load(f)
        faltantes = set(ingesta.etiquetas) - set(self.mapa["nodos"])
        if faltantes:
            raise ValueError(f"El mapa OPC-UA no define nodos para: {', '.join(sorted(faltantes))}")
        self.ingesta = ingesta
        self._etiqueta_por_nodo = {}

    def run(self):
        asyncio.run(self._ciclo())

    def _evento(self, texto, severidad):
        self.ingesta._evento("datos", texto, severidad)

    async def _ciclo(self):
        from asyncua import Client

        while True:
            try:
                cliente = Client(url=self.mapa["endpoint"])
                await self._configurar_seguridad(cliente)
                async with cliente:
                    nodos = {}
                    for etiqueta, nodo_id in self.mapa["nodos"].items():
                        nodo = cliente.get_node(nodo_id)
                        nodos[nodo] = etiqueta
                        self._etiqueta_por_nodo[nodo.nodeid] = etiqueta
                    sub = await cliente.create_subscription(self.mapa.get("intervalo_ms", 500), self)
                    # Con cola > 1 el servidor no descarta muestras entre publicaciones.
                    await sub.subscribe_data_change(list(nodos), queuesize=self.mapa.get("tamano_cola", 64))
                    self._evento(f"Conectado a OPC-UA {self.mapa['endpoint']} ({len(nodos)} etiquetas).", "info")
                    while True:
                        await asyncio.sleep(1)
                        await cliente.check_connection()
            except Exception as e:  # red, certificados, servidor caído: siempre reintentar
                self._evento(f"OPC-UA desconectado ({type(e).__name__}: {e}). Reintento en {self.REINTENTO_S} s.", "alta")
                await asyncio.sleep(self.REINTENTO_S)

    async def _configurar_seguridad(self, cliente):
        seg = self.mapa.get("seguridad")
        if seg:
            if seg.get("application_uri"):  # debe coincidir con la URI del certificado del cliente
                cliente.application_uri = seg["application_uri"]
            partes = [seg["politica"], seg["modo"], seg["certificado"], seg["llave"]]
            if seg.get("certificado_servidor"):
                partes.append(seg["certificado_servidor"])
            await cliente.set_security_string(",".join(partes))
        if self.mapa.get("usuario_env"):
            cliente.set_user(os.environ[self.mapa["usuario_env"]])
            cliente.set_password(os.environ[self.mapa.get("contrasena_env", "")])

    # Llamado por asyncua en cada cambio de valor suscrito.
    def datachange_notification(self, nodo, valor, datos):
        etiqueta = self._etiqueta_por_nodo.get(nodo.nodeid)
        dv = datos.monitored_item.Value
        marca = dv.SourceTimestamp or dv.ServerTimestamp
        if marca.tzinfo is None:
            marca = marca.replace(tzinfo=timezone.utc)
        self.ingesta.recibir(etiqueta, marca.timestamp(), valor)
