"""Save the README screenshots of the web app, on the demo pinned to one date.

Drives the Chrome (or Edge) already installed through the DevTools protocol, with the
``websockets`` client Streamlit already depends on (same approach as project 01). The upload
shot attaches a real file with mistakes to the file input, so the report it shows is the one a
user would get.

    python scripts/screenshots.py
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date
from pathlib import Path

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mto_bi import sample  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
DEMO_DATE = "2026-10-04"
APP_PORT, CDP_PORT = 8611, 9341
WIDTH = 1440
SHOTS = [  # (file, page, role, height, upload?)
    ("01-resumen.png", "resumen", "direccion", 1250, False),
    ("02-cotizaciones.png", "cotizaciones", "direccion", 1650, False),
    ("03-taller.png", "taller", "direccion", 1400, False),
    ("04-riesgo.png", "riesgo", "direccion", 1000, False),
    ("05-margen.png", "margen", "direccion", 1500, False),
    ("06-facturacion.png", "facturacion", "direccion", 1150, False),
    ("07-cargar.png", "cargar", "direccion", 1450, True),
]
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome",
    "chromium",
]
READY_JS = """
(() => !document.querySelector('[data-testid="stStatusWidget"]')
      && !document.querySelector('[data-testid="stSkeleton"]')
      && !!document.querySelector('h1'))()
"""


def find_browser() -> str:
    for b in BROWSERS:
        if Path(b).exists() or shutil.which(b):
            return b
    sys.exit("no Chrome/Edge/Chromium found")


def wait_http(url: str, timeout: float = 90) -> bytes:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return urllib.request.urlopen(url, timeout=2).read()
        except OSError:
            time.sleep(0.5)
    sys.exit(f"{url} did not answer")


class Page:
    def __init__(self, ws):
        self.ws, self.ids = ws, itertools.count(1)

    async def call(self, method: str, **params):
        msg_id = next(self.ids)
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            reply = json.loads(await self.ws.recv())
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    async def ready(self, timeout: float = 120) -> None:
        deadline, calm = time.time() + timeout, 0
        await asyncio.sleep(2)
        while time.time() < deadline:
            r = await self.call("Runtime.evaluate", expression=READY_JS, returnByValue=True)
            calm = calm + 1 if r["result"].get("value") else 0
            if calm >= 4:
                await asyncio.sleep(1.5)
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("page never finished running")

    async def attach(self, path: Path) -> None:
        object_id = None
        for _ in range(120):  # the page may still be building the example ZIP above it
            found = await self.call(
                "Runtime.evaluate", expression="document.querySelector('input[type=file]')"
            )
            object_id = found["result"].get("objectId")
            if object_id:
                break
            await asyncio.sleep(0.5)
        if not object_id:
            raise RuntimeError("no file input on the page")
        await self.call("DOM.setFileInputFiles", objectId=object_id, files=[str(path)])


async def shoot(upload: Path) -> None:
    targets = json.loads(wait_http(f"http://localhost:{CDP_PORT}/json/list"))
    ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
    async with connect(ws_url, max_size=200 * 1024 * 1024) as ws:
        page = Page(ws)
        await page.call("Page.enable")
        await page.call("Runtime.enable")
        await page.call("DOM.enable")
        for name, path, role, height, with_upload in SHOTS:
            await page.call(
                "Emulation.setDeviceMetricsOverride",
                width=WIDTH,
                height=height,
                deviceScaleFactor=1,
                mobile=False,
            )
            # A fresh session per shot, so ?ver= picks the role.
            await page.call("Network.clearBrowserCookies")
            url = f"http://localhost:{APP_PORT}/{'' if path == 'resumen' else path}?ver={role}"
            await page.call("Page.navigate", url="about:blank")
            await page.call("Page.navigate", url=url)
            await page.ready()
            if with_upload:
                await page.attach(upload)
                await page.ready()
            shot = await page.call("Page.captureScreenshot", format="png")
            (OUT / name).write_bytes(base64.b64decode(shot["data"]))
            print(f"{name:28} {url}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())
    files = dict(sample.files(sample.generate(date.fromisoformat(DEMO_DATE))))
    upload = work / "pedidos_octubre.xlsx"
    upload.write_bytes(files["pedidos.xlsx"])  # carries the demo's typing mistakes
    env = {**os.environ, "MTO_DEMO_DATE": DEMO_DATE}
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ROOT / "streamlit_app.py"),
            "--server.port",
            str(APP_PORT),
            "--server.headless",
            "true",
            "--theme.base",
            "light",
            "--browser.gatherUsageStats",
            "false",
            "--client.toolbarMode",
            "minimal",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    profile = tempfile.mkdtemp()
    browser = None
    try:
        wait_http(f"http://localhost:{APP_PORT}/_stcore/health")
        browser = subprocess.Popen(
            [
                find_browser(),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={profile}",
                f"--window-size={WIDTH},1300",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        asyncio.run(shoot(upload))
    finally:
        if browser:
            browser.terminate()
            browser.wait(timeout=10)
        server.terminate()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
