"""Command line: ``mto sample | templates | check | app``."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

from mto_bi import dataset, sample
from mto_bi.schema import TEMPLATES
from mto_bi.templates import workbook_bytes


def _sample(args) -> int:
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    for name in sample.write(args.out, as_of):
        print(Path(args.out) / name)
    return 0


def _templates(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for t in TEMPLATES:
        path = out / f"plantilla_{t.name}.xlsx"
        path.write_bytes(workbook_bytes(t))
        print(path)
    return 0


def _check(args) -> int:
    files = []
    for p in map(Path, args.files):
        paths = sorted(p.iterdir()) if p.is_dir() else [p]
        files += [(x.name, x.read_bytes()) for x in paths if x.is_file()]
    d = dataset.build(dataset.classify(files))
    for r in d.report:
        print(
            f"{r.table:<14} {r.file:<30} leídas {r.rows_read:>6}  cargadas {r.rows_loaded:>6}  "
            f"con error {r.rows_rejected:>4}"
        )
    for i in d.issues:
        where = f"fila {i.row}" if i.row else ""
        print(f"[{i.severity}] {i.file} {where} {i.column}: {i.message}")
    print(f"Datos al {d.as_of}")
    return 1 if any(i.severity == "error" for i in d.issues) else 0


def _app(args) -> int:
    app = Path(__file__).resolve().parents[2] / "streamlit_app.py"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    print(f"http://localhost:{args.port}  (Ctrl+C para detener)")
    return subprocess.call(cmd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mto", description="Tablero para un taller de muebles a la medida"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample", help="escribe los archivos de la demostración")
    p.add_argument("--out", default="data/demo")
    p.add_argument("--as-of", help="AAAA-MM-DD (por omisión, hoy)")
    p.set_defaults(func=_sample)
    p = sub.add_parser("templates", help="escribe las cuatro plantillas vacías")
    p.add_argument("--out", default="data/plantillas")
    p.set_defaults(func=_templates)
    p = sub.add_parser("check", help="revisa archivos o carpetas como lo hace «Cargar datos»")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=_check)
    p = sub.add_parser("app", help="abre el tablero")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(func=_app)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
