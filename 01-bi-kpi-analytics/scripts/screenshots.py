"""Save the README screenshots of the dashboard.

Drives the Chrome (or Edge) already installed on the machine through the DevTools protocol,
with the ``websockets`` client Streamlit already depends on: no browser download and no extra
package. Each shot waits until Streamlit has finished running the page — plain
`chrome --headless --screenshot` does not: its virtual clock shoots half-rendered pages.

    python scripts/screenshots.py            # after `bi load` with the real data
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
APP_PORT, CDP_PORT = 8599, 9339
WIDTH = 1440
SHOTS = [  # (file, page, user, height)
    ("01-resumen.png", "resumen", "direccion", 1300),
    ("02-ventas.png", "ventas", "direccion", 1420),
    ("03-operacion.png", "operacion", "direccion", 1500),
    ("04-alertas.png", "alertas", "direccion", 1250),
    ("05-calidad.png", "calidad", "direccion", 1300),
    ("06-gerente-nordeste.png", "resumen", "gerente.nordeste", 1300),
    ("07-vendedor.png", "resumen", "vendedor.4869f7a5", 1300),
]
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome",
    "chromium",
]
# Streamlit has finished when its "Running…" widget and loading skeletons are gone.
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


def wait_http(url: str, timeout: float = 60) -> bytes:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return urllib.request.urlopen(url, timeout=2).read()
        except OSError:
            time.sleep(0.5)
    sys.exit(f"{url} did not answer")


class Page:
    """Minimal DevTools client for one page: request/response matched by id."""

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

    async def ready(self, timeout: float = 90) -> None:
        deadline, calm = time.time() + timeout, 0
        await asyncio.sleep(2)
        while time.time() < deadline:
            r = await self.call("Runtime.evaluate", expression=READY_JS, returnByValue=True)
            calm = calm + 1 if r["result"].get("value") else 0
            if calm >= 4:  # idle for 2 s: charts drawn, no rerun pending
                await asyncio.sleep(1.5)
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("page never finished running")


async def shoot() -> None:
    targets = json.loads(wait_http(f"http://localhost:{CDP_PORT}/json/list"))
    ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
    async with connect(ws_url, max_size=200 * 1024 * 1024) as ws:
        await _shoot_all(Page(ws))


async def _shoot_all(page: Page) -> None:
    await page.call("Page.enable")
    await page.call("Runtime.enable")
    for name, path, user, height in SHOTS:
        await page.call(
            "Emulation.setDeviceMetricsOverride",
            width=WIDTH,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )
        url = f"http://localhost:{APP_PORT}/{'' if path == 'resumen' else path}?usuario={user}"
        await page.call("Page.navigate", url=url)
        await page.ready()
        shot = await page.call("Page.captureScreenshot", format="png")
        (OUT / name).write_bytes(base64.b64decode(shot["data"]))
        print(f"{name:28} {url}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    app = ROOT / "src" / "bi_kpi" / "dashboard" / "app.py"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app),
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
        asyncio.run(shoot())
    finally:
        if browser:
            browser.terminate()
            browser.wait(timeout=10)
        server.terminate()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
