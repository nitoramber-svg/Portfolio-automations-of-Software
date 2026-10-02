"""Get the Olist CSVs onto disk: from the Kaggle API or from a ZIP the user downloaded."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import requests

from bi_kpi.olist import FILES, OPTIONAL_TABLES

KAGGLE_DOWNLOAD_URL = "https://www.kaggle.com/api/v1/datasets/download/{dataset}"


class DownloadError(RuntimeError):
    pass


def extract_zip(zip_path: Path, raw_dir: Path) -> list[Path]:
    """Unpack the Olist CSVs from ``zip_path`` into ``raw_dir``, ignoring folders inside the ZIP."""
    expected = {name for name, _ in FILES.values()}
    raw_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = Path(member.filename).name
            if name in expected:
                target = raw_dir / name
                target.write_bytes(zf.read(member))
                written.append(target)
    optional = {FILES[t][0] for t in OPTIONAL_TABLES}
    missing = expected - optional - {p.name for p in written}
    if missing:
        raise DownloadError(f"{zip_path} is missing Olist files: {sorted(missing)}")
    return written


def download_from_kaggle(dataset: str, raw_dir: Path, timeout: int = 300) -> list[Path]:
    """Download ``dataset`` with the credentials in KAGGLE_USERNAME / KAGGLE_KEY."""
    user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    if not user or not key:
        raise DownloadError(
            "Set KAGGLE_USERNAME and KAGGLE_KEY, or pass --zip with the archive "
            "downloaded from https://www.kaggle.com/datasets/" + dataset
        )
    zip_path = raw_dir.parent / "olist.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(
        KAGGLE_DOWNLOAD_URL.format(dataset=dataset),
        auth=(user, key),
        stream=True,
        timeout=timeout,
    ) as resp:
        if resp.status_code in (401, 403):
            raise DownloadError(
                "Kaggle rejected the credentials (check KAGGLE_USERNAME/KAGGLE_KEY)"
            )
        resp.raise_for_status()
        with zip_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    return extract_zip(zip_path, raw_dir)
