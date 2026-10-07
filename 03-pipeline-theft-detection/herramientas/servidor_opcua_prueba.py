"""Servidor OPC-UA de prueba: publica las lecturas del simulador como lo haría un SCADA.

    python herramientas/servidor_opcua_prueba.py --velocidad 20
    python herramientas/servidor_opcua_prueba.py --velocidad 20 --seguro certs/

Con --seguro genera certificados de prueba para servidor y cliente, solo acepta
Basic256Sha256 + SignAndEncrypt y escribe certs/mapa_seguro.json para el cliente.

Expone los nodos definidos en el mapa (por defecto sentinela/datos/mapa_opcua_ejemplo.json),
con la marca de tiempo de origen de cada muestra. Requiere: pip install asyncua
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asyncua import Server, ua  # noqa: E402
from asyncua.crypto.cert_gen import setup_self_signed_certificate  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID  # noqa: E402

from sentinela import config  # noqa: E402
from sentinela.simulador import Simulador  # noqa: E402


async def main(args):
    cfg = config.cargar_ducto()
    escenario = config.cargar_escenario(args.escenario)
    with open(args.mapa, encoding="utf-8") as f:
        mapa = json.load(f)

    servidor = Server()
    await servidor.init()
    servidor.set_endpoint(mapa["endpoint"])
    servidor.set_server_name("SCADA de prueba Sentinela")
    if args.seguro:
        await _configurar_seguridad(servidor, mapa, Path(args.seguro))
    idx = await servidor.register_namespace("urn:sentinela:scada-prueba:nodos")
    carpeta = await servidor.nodes.objects.add_folder(idx, cfg["ducto"]["id"])
    variables = {}
    for etiqueta, nodo_id in mapa["nodos"].items():
        nid = ua.NodeId.from_string(nodo_id)
        if nid.NamespaceIndex != idx:
            raise SystemExit(f"El mapa usa ns={nid.NamespaceIndex} pero el servidor registró ns={idx}.")
        variables[etiqueta] = await carpeta.add_variable(nid, etiqueta, 0.0)

    sim = Simulador(cfg, escenario)
    async with servidor:
        print(f"SCADA de prueba en {mapa['endpoint']} ({len(variables)} etiquetas, {args.velocidad}x)", flush=True)
        inicio, inicio_mono = time.time(), time.monotonic()
        for t in range(sim.duracion):
            espera = inicio_mono + t / args.velocidad - time.monotonic()
            if espera > 0:
                await asyncio.sleep(espera)
            lectura = sim.leer(t)
            marca = datetime.fromtimestamp(inicio + t, timezone.utc)
            valores = {**lectura.presiones, "q_entrada": lectura.q_entrada, "q_salida": lectura.q_salida}
            for etiqueta, valor in valores.items():
                await variables[etiqueta].write_value(
                    ua.DataValue(ua.Variant(float(valor), ua.VariantType.Double), SourceTimestamp=marca, ServerTimestamp=marca))
            if t % 300 == 0:
                print(f"t={t:>5} s publicado", flush=True)
        print("Escenario terminado; el servidor sigue arriba (Ctrl+C para salir).", flush=True)
        while True:
            await asyncio.sleep(3600)


async def _configurar_seguridad(servidor, mapa, carpeta):
    carpeta.mkdir(parents=True, exist_ok=True)
    sujeto = {"countryName": "MX", "organizationName": "Sentinela (pruebas)"}
    uri_servidor, uri_cliente = "urn:sentinela:scada-prueba", "urn:sentinela:cliente"
    await setup_self_signed_certificate(carpeta / "servidor_llave.pem", carpeta / "servidor.der", uri_servidor,
                                        "127.0.0.1", [ExtendedKeyUsageOID.SERVER_AUTH], sujeto)
    await setup_self_signed_certificate(carpeta / "cliente_llave.pem", carpeta / "cliente.der", uri_cliente,
                                        "127.0.0.1", [ExtendedKeyUsageOID.CLIENT_AUTH], sujeto)
    servidor.set_server_name("SCADA de prueba Sentinela (cifrado)")
    await servidor.set_application_uri(uri_servidor)
    servidor.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
    await servidor.load_certificate(str(carpeta / "servidor.der"))
    await servidor.load_private_key(str(carpeta / "servidor_llave.pem"))
    seguro = {**mapa, "seguridad": {
        "politica": "Basic256Sha256", "modo": "SignAndEncrypt", "application_uri": uri_cliente,
        "certificado": str(carpeta / "cliente.der"), "llave": str(carpeta / "cliente_llave.pem"),
        "certificado_servidor": str(carpeta / "servidor.der")}}
    with open(carpeta / "mapa_seguro.json", "w", encoding="utf-8") as f:
        json.dump(seguro, f, ensure_ascii=False, indent=2)
    print(f"Modo cifrado: solo Basic256Sha256/SignAndEncrypt. Mapa del cliente: {carpeta / 'mapa_seguro.json'}", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mapa", default=config.DIR_DATOS + "/mapa_opcua_ejemplo.json")
    p.add_argument("--escenario")
    p.add_argument("--velocidad", type=float, default=1.0)
    p.add_argument("--seguro", metavar="CARPETA", help="genera certificados y exige cifrado")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
