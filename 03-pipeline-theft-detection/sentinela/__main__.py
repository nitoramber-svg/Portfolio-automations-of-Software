"""Línea de comandos.

    python -m sentinela demo        tablero en vivo con el escenario de ejemplo
    python -m sentinela servidor    modo tiempo real: recibe lecturas por HTTP u OPC-UA
    python -m sentinela usuarios    alta, baja y lista de usuarios del tablero
    python -m sentinela vigia       avisa a la guardia (webhook/Telegram) si el sistema falla o hay incidentes
    python -m sentinela emisor      envía lecturas simuladas por la red (prueba del modo servidor)
    python -m sentinela simular     genera un CSV con lecturas SCADA sintéticas
    python -m sentinela analizar    procesa un CSV histórico y emite el reporte
    python -m sentinela evaluar     compara un histórico contra tomas confirmadas en campo
    python -m sentinela campana     barrido Monte Carlo de sensibilidad y falsas alarmas
"""
import argparse
import getpass
import os
import sys
import time
import webbrowser

from . import config
from .almacen import RespaldoPeriodico
from .auth import ROLES, Sesiones, Usuarios
from .ejecucion import EjecutorSimulacion, analizar_csv, crear_sistema, exportar_csv
from .ingesta import BombaIngesta, Ingesta
from .servidor import Aplicacion, crear_servidor
from .simulador import Simulador

LOCALES = ("127.0.0.1", "localhost", "::1")


def _ruta_bd(args):
    if args.bd is None:
        os.makedirs("registros", exist_ok=True)
        args.bd = os.path.join("registros", time.strftime("turno_%Y%m%d_%H%M%S.db"))
    print(f"Registro auditable del turno: {args.bd}")
    return args.bd


def _preparar_seguridad(app, args, host):
    """Usuarios, HTTPS y respaldos. Exponer el tablero a la red exige usuarios y cifrado."""
    remoto = host not in LOCALES
    if args.usuarios:
        usuarios = Usuarios(args.usuarios)
        if not usuarios.listar():
            raise SystemExit(f"{args.usuarios} no tiene usuarios. Cree uno: python -m sentinela usuarios "
                             f"--archivo {args.usuarios} agregar NOMBRE --rol admin")
        app.sesiones = Sesiones(usuarios)
        print(f"Inicio de sesión activado ({len(usuarios.listar())} usuarios en {args.usuarios}).")
    elif remoto:
        raise SystemExit("Para escuchar en la red se requiere --usuarios (inicio de sesión con roles).")
    if (args.certificado is None) != (args.llave is None):
        raise SystemExit("--certificado y --llave van juntos.")
    if remoto and not args.certificado and not args.permitir_http_remoto:
        raise SystemExit("Para escuchar en la red se requiere HTTPS (--certificado y --llave), o --permitir-http-remoto "
                         "si un proxy inverso ya cifra la conexión.")
    if args.bd != ":memory:" and args.respaldo_cada > 0:
        directorio = os.path.join(os.path.dirname(os.path.abspath(args.bd)), "respaldos")

        def al_fallar(texto):
            with app.monitor.lock:
                app.gestor.registrar_evento(app.monitor.t, "datos", texto, "alta")

        app.respaldos = RespaldoPeriodico(app.gestor.almacen, directorio, args.respaldo_cada * 60,
                                          args.conservar_respaldos, al_fallar)
        app.respaldos.start()
        print(f"Respaldos cada {args.respaldo_cada:g} min en {directorio} (se conservan {args.conservar_respaldos}).")


def _servir(app, args, host):
    servidor = crear_servidor(app, args.puerto, host, args.certificado, args.llave)
    esquema = "https" if args.certificado else "http"
    url = f"{esquema}://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{args.puerto}/"
    print(f"Sentinela Ductos en {url}  (Ctrl+C para salir)")
    if not args.sin_navegador:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_demo(args):
    cfg = config.cargar_ducto(args.ducto)
    escenario = config.cargar_escenario(args.escenario)
    monitor, gestor = crear_sistema(cfg, _ruta_bd(args))
    app = Aplicacion(cfg, monitor, gestor, inicio_epoch=time.time())
    _preparar_seguridad(app, args, "127.0.0.1")
    app.ejecutor = EjecutorSimulacion(Simulador(cfg, escenario), monitor, args.velocidad)
    print(f"Escenario: {escenario.get('descripcion', '')}")
    app.ejecutor.start()
    _servir(app, args, "127.0.0.1")


def cmd_servidor(args):
    cfg = config.cargar_ducto(args.ducto)
    monitor, gestor = crear_sistema(cfg, _ruta_bd(args))
    token = args.token or os.environ.get("SENTINELA_TOKEN")
    if not token and args.host not in LOCALES:
        raise SystemExit("Para escuchar fuera de 127.0.0.1 se requiere --token o SENTINELA_TOKEN.")
    almacen = gestor.almacen
    ingesta = Ingesta(cfg, monitor, latencia_s=args.latencia, max_hueco_s=args.max_hueco,
                      verificar_reloj=not args.sin_verificar_reloj,
                      inicio=almacen.leer_meta("inicio_epoch"),
                      al_fijar_inicio=lambda s: almacen.escribir_meta("inicio_epoch", s))
    if ingesta.inicio is not None:
        print(f"Continuando el turno guardado en {args.bd} ({len(gestor.incidentes)} incidentes).")
    app = Aplicacion(cfg, monitor, gestor, ingesta=ingesta, token=token)
    _preparar_seguridad(app, args, args.host)
    BombaIngesta(ingesta).start()
    if args.opcua:
        from .fuente_opcua import FuenteOPCUA
        FuenteOPCUA(args.opcua, ingesta).start()
        print(f"Fuente OPC-UA: {args.opcua}")
    print("Esperando lecturas en POST /api/lecturas" + (" (requiere token)" if token else " (sin token: solo uso local)"))
    _servir(app, args, args.host)


def cmd_usuarios(args):
    usuarios = Usuarios(args.archivo)
    try:
        if args.accion == "listar":
            filas = usuarios.listar()
            print("\n".join(f"{u:<24} {r}" for u, r in filas) if filas else "Sin usuarios.")
        elif args.accion == "eliminar":
            usuarios.eliminar(args.nombre)
            print(f"Usuario {args.nombre} eliminado.")
        else:
            if args.contrasena_env:
                contrasena = os.environ.get(args.contrasena_env, "")
            else:
                contrasena = getpass.getpass("Contraseña: ")
                if contrasena != getpass.getpass("Repítala: "):
                    raise ValueError("Las contraseñas no coinciden.")
            usuarios.agregar(args.nombre, contrasena, args.rol)
            print(f"Usuario {args.nombre} guardado con rol {args.rol} en {args.archivo}.")
    except ValueError as e:
        raise SystemExit(str(e))


def cmd_vigia(args):
    from .vigia import Vigia, notificador_telegram, notificador_webhook
    canales = []
    if args.webhook:
        canales.append(notificador_webhook(args.webhook))
    if args.telegram_chat:
        token_bot = os.environ.get("SENTINELA_TELEGRAM_TOKEN")
        if not token_bot:
            raise SystemExit("Defina SENTINELA_TELEGRAM_TOKEN con el token del bot de Telegram.")
        canales.append(notificador_telegram(token_bot, args.telegram_chat))
    if not canales:
        print("Sin canales (--webhook / --telegram-chat): los avisos solo se imprimen aquí.")
    vigia = Vigia(args.url, canales, args.token or os.environ.get("SENTINELA_TOKEN"),
                  args.fallos, args.inseguro)
    print(f"Vigilando {args.url} cada {args.cada:g} s.")
    try:
        vigia.correr(args.cada)
    except KeyboardInterrupt:
        pass


def cmd_emisor(args):
    from .emisor import emitir
    if args.velocidad > 1:
        print("Aviso: a velocidad > 1 las marcas de tiempo van adelante del reloj; "
              "inicie el servidor con --sin-verificar-reloj.")
    stats = emitir(config.cargar_ducto(args.ducto), config.cargar_escenario(args.escenario), args.url,
                   args.token or os.environ.get("SENTINELA_TOKEN"), args.velocidad, args.perdida,
                   args.desorden, args.basura)
    print(f"Fin del envío: {dict(stats)}")


def cmd_simular(args):
    cfg = config.cargar_ducto(args.ducto)
    bitacora = exportar_csv(cfg, config.cargar_escenario(args.escenario), args.salida)
    print(f"Lecturas: {args.salida}\nBitácora operativa: {bitacora}")


def cmd_analizar(args):
    cfg = config.cargar_ducto(args.ducto)
    bitacora = args.bitacora
    if bitacora is None:
        candidata = args.archivo.rsplit(".", 1)[0] + ".bitacora.csv"
        bitacora = candidata if os.path.exists(candidata) else None
    _, gestor = analizar_csv(cfg, args.archivo, bitacora)
    reloj = lambda t: time.strftime("%H:%M:%S", time.gmtime(t))
    print("\n== EVENTOS ==")
    for e in gestor.eventos:
        print(f"[{reloj(e['t'])}] {e['tipo']:<15} {e['descripcion']}")
    print("\n== INCIDENTES ==")
    for i in reversed(gestor.lista()):
        print(f"#{i['id']} {i['tipo_texto']}\n"
              f"   ubicación: km {i['km']:.1f} ± {i['incertidumbre_km']:.1f}  ({i['acceso']['nombre']})\n"
              f"   detectado: {reloj(i['t_deteccion'])}, {i['t_deteccion'] - i['t_inicio_est']:.0f} s después del inicio estimado\n"
              f"   volumen estimado: {i['litros']:,} L   pérdida: ${i['perdida_mxn']:,} MXN\n"
              f"   brigada sugerida: {i['brigada_sugerida']['nombre']} (ETA {i['brigada_sugerida']['eta_min']} min)")
    k = gestor.kpis()
    print(f"\nTotal: {k['total']} incidentes, {k['litros']:,} L, ${k['perdida_mxn']:,} MXN")


def cmd_evaluar(args):
    from .evaluacion import evaluar_csv
    r = evaluar_csv(config.cargar_ducto(args.ducto), args.archivo, args.tomas, args.bitacora, args.tolerancia_km)
    fmt = lambda v, f="{:.2f}": "—" if v is None else f.format(v)
    print(f"Tomas conocidas: {r['tomas']}  ·  detectadas: {r['detectadas']}  ·  tasa: {fmt(r['tasa_deteccion'], '{:.0%}')}")
    print(f"Error de ubicación: medio {fmt(r['error_km_medio'])} km, p90 {fmt(r['error_km_p90'])} km")
    print(f"Tiempo de detección: mediana {fmt(r['tiempo_s_mediana'], '{:.0f}')} s, p90 {fmt(r['tiempo_s_p90'], '{:.0f}')} s")
    print(f"Incidentes sin toma conocida: {r['falsas_alarmas']} ({fmt(r['falsas_por_dia'])} por día) -> "
          f"revisarlos en campo: pueden ser falsas alarmas o tomas que nadie había encontrado")
    for d in r["detalle"]:
        estado = f"detectada (incidente #{d['incidente']}, {d['error_km']:.2f} km, {d['tiempo_s']:.0f} s)" if d["detectada"] else "NO detectada"
        print(f"  t={d['t_inicio']:.0f} km {d['km']:.1f}: {estado}")


def cmd_campana(args):
    from .evaluacion import campana, reporte_campana
    print(f"Corriendo {args.corridas} tomas simuladas + {args.dias} día(s) de operación normal...")
    resultados, normal = campana(config.cargar_ducto(args.ducto), config.cargar_escenario(args.escenario),
                                 args.corridas, args.dias)
    print(reporte_campana(resultados, normal))


def _opciones_seguridad(p):
    p.add_argument("--usuarios", help="JSON de usuarios: activa inicio de sesión y roles")
    p.add_argument("--certificado", help="certificado TLS (PEM) para servir por HTTPS")
    p.add_argument("--llave", help="llave privada TLS (PEM)")
    p.add_argument("--permitir-http-remoto", action="store_true",
                   help="permite escuchar en la red sin HTTPS (solo detrás de un proxy que cifre)")
    p.add_argument("--respaldo-cada", type=float, default=60, help="minutos entre respaldos de la base (0 = nunca)")
    p.add_argument("--conservar-respaldos", type=int, default=48)


def main():
    # La consola de Windows usa cp1252 por defecto y rompe los acentos.
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="sentinela", description="Detección y localización de tomas clandestinas en ductos.")
    p.add_argument("--ducto", help="JSON de configuración del ducto (por defecto, el ducto demo)")
    sub = p.add_subparsers(dest="comando", required=True)

    d = sub.add_parser("demo", help="Tablero en vivo con simulación")
    d.add_argument("--escenario")
    d.add_argument("--velocidad", type=int, default=30, help="segundos simulados por segundo real")
    d.add_argument("--puerto", type=int, default=8765)
    d.add_argument("--bd", help="archivo SQLite del turno (por defecto, uno nuevo en registros/)")
    d.add_argument("--sin-navegador", action="store_true")
    _opciones_seguridad(d)
    d.set_defaults(func=cmd_demo)

    v = sub.add_parser("servidor", help="Modo tiempo real: recibe lecturas por HTTP y/o OPC-UA")
    v.add_argument("--puerto", type=int, default=8765)
    v.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para aceptar conexiones de la red (exige token)")
    v.add_argument("--token", help="token para POST /api/lecturas (o variable SENTINELA_TOKEN)")
    v.add_argument("--bd", help="archivo SQLite del turno (por defecto, uno nuevo en registros/)")
    v.add_argument("--opcua", help="JSON con endpoint y nodos OPC-UA (requiere pip install asyncua)")
    v.add_argument("--latencia", type=float, default=3, help="segundos de espera a lecturas rezagadas")
    v.add_argument("--max-hueco", type=float, default=10, help="segundos que se rellena una etiqueta sin datos")
    v.add_argument("--sin-verificar-reloj", action="store_true", help="acepta marcas de tiempo futuras (reproducción acelerada)")
    v.add_argument("--sin-navegador", action="store_true")
    _opciones_seguridad(v)
    v.set_defaults(func=cmd_servidor)

    u = sub.add_parser("usuarios", help="Alta, baja y lista de usuarios del tablero")
    u.add_argument("--archivo", default="usuarios.json")
    us = u.add_subparsers(dest="accion", required=True)
    ua = us.add_parser("agregar", help="crea o actualiza un usuario (pide la contraseña)")
    ua.add_argument("nombre")
    ua.add_argument("--rol", choices=ROLES, required=True)
    ua.add_argument("--contrasena-env", help="toma la contraseña de esta variable de entorno (automatización)")
    ue = us.add_parser("eliminar")
    ue.add_argument("nombre")
    us.add_parser("listar")
    u.set_defaults(func=cmd_usuarios)

    g = sub.add_parser("vigia", help="Avisa a la guardia si el sistema falla o aparece un incidente")
    g.add_argument("--url", default="http://127.0.0.1:8765")
    g.add_argument("--token", help="token de máquina para leer incidentes (o SENTINELA_TOKEN)")
    g.add_argument("--cada", type=float, default=30, help="segundos entre revisiones")
    g.add_argument("--fallos", type=int, default=2, help="revisiones fallidas seguidas antes de avisar")
    g.add_argument("--webhook", help="URL que recibe POST con {text: ...} (Slack, Teams, Discord, n8n...)")
    g.add_argument("--telegram-chat", help="chat_id de Telegram (token del bot en SENTINELA_TELEGRAM_TOKEN)")
    g.add_argument("--inseguro", action="store_true", help="acepta certificados autofirmados")
    g.set_defaults(func=cmd_vigia)

    m = sub.add_parser("emisor", help="Envía lecturas simuladas por HTTP a un servidor en modo tiempo real")
    m.add_argument("--url", default="http://127.0.0.1:8765")
    m.add_argument("--token")
    m.add_argument("--escenario")
    m.add_argument("--velocidad", type=float, default=1.0)
    m.add_argument("--perdida", type=float, default=0.0, help="fracción de lecturas que se pierden (0–1)")
    m.add_argument("--desorden", action="store_true", help="envía lecturas desordenadas y con retraso")
    m.add_argument("--basura", action="store_true", help="intercala lecturas inválidas para probar la validación")
    m.set_defaults(func=cmd_emisor)

    s = sub.add_parser("simular", help="Exporta lecturas sintéticas a CSV")
    s.add_argument("--escenario")
    s.add_argument("--salida", default="lecturas.csv")
    s.set_defaults(func=cmd_simular)

    a = sub.add_parser("analizar", help="Analiza un CSV histórico del SCADA")
    a.add_argument("archivo")
    a.add_argument("--bitacora", help="CSV t,descripcion con las maniobras registradas")
    a.set_defaults(func=cmd_analizar)

    e = sub.add_parser("evaluar", help="Métricas de detección contra tomas confirmadas en campo")
    e.add_argument("archivo", help="CSV histórico t,E0..En,q_entrada,q_salida")
    e.add_argument("--tomas", required=True, help="CSV t_inicio,km[,caudal_m3h,descripcion] de tomas confirmadas")
    e.add_argument("--bitacora")
    e.add_argument("--tolerancia-km", type=float, default=5.0)
    e.set_defaults(func=cmd_evaluar)

    c = sub.add_parser("campana", help="Monte Carlo: sensibilidad, error de ubicación y falsas alarmas")
    c.add_argument("--escenario")
    c.add_argument("--corridas", type=int, default=160)
    c.add_argument("--dias", type=float, default=2, help="días de operación normal sin tomas")
    c.set_defaults(func=cmd_campana)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
