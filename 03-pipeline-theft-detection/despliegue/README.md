# Guía de despliegue y operación 24/7

## 1. Arquitectura de red (IEC 62443)

```
 Zona de control (OT)              Zona intermedia (DMZ industrial)            Zona corporativa
 ┌──────────────────┐   OPC-UA     ┌──────────────────────────────┐   HTTPS    ┌──────────────────┐
 │ SCADA / RTU      │ ───────────► │ Sentinela (servidor)         │ ◄───────── │ Operadores       │
 │ servidor OPC-UA  │  cifrado,    │ solo LEE del SCADA           │  usuarios  │ (navegador)      │
 └──────────────────┘  solo        │ SQLite + respaldos           │  y roles   └──────────────────┘
                       lectura     └──────────────────────────────┘
                                                 ▲ /api/salud, /api/incidentes (token)
                                   ┌─────────────┴────────────────┐
                                   │ Vigía (otro equipo)          │ ──► Telegram / webhook de guardia
                                   └──────────────────────────────┘
```

- **Sentinela nunca escribe en el SCADA.** Solo se suscribe a lecturas, así que no puede alterar la operación del ducto aunque fuera comprometido.
- El firewall deja pasar únicamente **DMZ → OPC-UA (4840)** y **corporativa → HTTPS (8443)**. Ninguna conexión entra desde la red corporativa a la zona de control.
- OPC-UA va con `Basic256Sha256` + `SignAndEncrypt` y certificados (sección `seguridad` del mapa). Las credenciales viven en variables de entorno, no en archivos del repositorio.

## 2. Instalación (Linux)

```bash
sudo useradd --system --home /opt/sentinela sentinela
sudo mkdir -p /opt/sentinela /etc/sentinela/tls /var/lib/sentinela
sudo chown sentinela: /var/lib/sentinela
cd /opt/sentinela && python3 -m venv .venv && .venv/bin/pip install "sentinela-ductos[opcua] @ <ruta o repo>"
```

**Certificado HTTPS.** Lo ideal es usar la CA interna de la empresa. Para pruebas, uno autofirmado:

```bash
openssl req -x509 -newkey rsa:3072 -nodes -days 365 -subj "/CN=sentinela.interno" \
  -keyout /etc/sentinela/tls/llave.pem -out /etc/sentinela/tls/cert.pem
chmod 600 /etc/sentinela/tls/llave.pem
```

**Usuarios.** Las contraseñas se guardan con PBKDF2 y nunca en claro. Las altas y bajas se aplican sin reiniciar el servicio.

```bash
sentinela usuarios --archivo /etc/sentinela/usuarios.json agregar jefe.turno --rol supervisor
sentinela usuarios --archivo /etc/sentinela/usuarios.json agregar monitorista1 --rol operador
sentinela usuarios --archivo /etc/sentinela/usuarios.json agregar auditoria --rol lectura
```

| Rol | Ver | Despachar / en sitio | Cerrar incidentes | Controlar simulación |
|---|---|---|---|---|
| lectura | ✔ | | | |
| operador | ✔ | ✔ | | |
| supervisor | ✔ | ✔ | ✔ | ✔ |
| admin | ✔ | ✔ | ✔ | ✔ |

**Servicio.** Copia `sentinela.service` a `/etc/systemd/system/` y ejecuta `systemctl enable --now sentinela`. Así arranca con el equipo, se reinicia solo si falla y queda restringido a escribir únicamente en `/var/lib/sentinela`.

El servidor **se niega a escuchar en la red** si falta alguno de estos: usuarios, HTTPS (o un proxy que ya cifre, con `--permitir-http-remoto`) o el token de máquina.

## 3. Respaldos y recuperación

- La base del turno se respalda en caliente cada `--respaldo-cada` minutos en `respaldos/`, y se conservan los últimos `--conservar-respaldos`.
- Copia esa carpeta fuera del equipo (rsync o almacenamiento de objetos) con un cron.
- **Reinicio o corte de luz:** al arrancar con la misma `--bd`, el sistema recupera los incidentes, la bitácora y la base de tiempo, y recalibra los detectores.
- **Pérdida del disco:** copia el respaldo más reciente como `--bd` y arranca. Se pierde como máximo el intervalo entre respaldos.

## 4. Redundancia

**Activo–pasivo**, que es lo recomendado para un piloto:

1. Dos equipos con Sentinela, ambos suscritos al SCADA. Como OPC-UA es de solo lectura, el SCADA puede atender a dos clientes sin problema.
2. Los operadores entran al **activo** mediante un nombre DNS o una IP virtual.
3. Un vigía revisa `/api/salud` del activo. Si cae, el jefe de turno cambia el DNS o la IP al pasivo, que ya trae los datos al día. Esto se puede automatizar con keepalived.
4. Los incidentes se gestionan en el activo. El pasivo recupera el historial restaurando el último respaldo.

Para varios ductos o alta disponibilidad real hace falta migrar el almacén a PostgreSQL con réplica. El almacén está aislado en `almacen.py` para que ese cambio sea local.

## 5. Guardia 24/7 con el vigía

```bash
export SENTINELA_TOKEN=...            # token de máquina (solo lectura)
export SENTINELA_TELEGRAM_TOKEN=...   # bot de Telegram
sentinela vigia --url https://sentinela.interno:8443 --telegram-chat <chat_id>
```

- Avisa una sola vez si el servidor deja de responder o reporta "degradado" (enlace SCADA caído, etiquetas sin datos, respaldo fallido), y avisa de nuevo cuando se restablece.
- Avisa **cada incidente nuevo** con km, acceso y brigada sugerida, aunque nadie esté mirando el tablero.
- Córrelo en un equipo distinto al servidor; de lo contrario no puede avisar si ese equipo se apaga.
