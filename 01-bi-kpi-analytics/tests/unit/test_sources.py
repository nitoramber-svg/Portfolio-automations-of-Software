from __future__ import annotations

import zipfile

import duckdb
import pytest

from bi_kpi import sample
from bi_kpi.download import DownloadError, extract_zip
from bi_kpi.extract.files import SourceContractError, load_csv
from bi_kpi.olist import FILES


def test_missing_column_is_rejected(settings):
    path = settings.raw_dir / FILES["sellers"][0]
    path.write_text("seller_id,seller_city\ns1,campinas\n", encoding="utf-8")
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw")
    with pytest.raises(SourceContractError, match="seller_zip_code_prefix"):
        load_csv(con, "sellers", settings.raw_dir)


def test_extract_zip_accepts_nested_folders_and_no_geolocation(settings, tmp_path):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in settings.raw_dir.glob("*.csv"):
            zf.write(path, f"archive/{path.name}")
    out = tmp_path / "out"
    written = extract_zip(archive, out)
    assert len(written) == 8


def test_extract_zip_reports_missing_files(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("olist_orders_dataset.csv", "order_id\n")
    with pytest.raises(DownloadError, match="olist_customers_dataset.csv"):
        extract_zip(archive, tmp_path / "out")


def test_sample_is_deterministic_and_follows_the_contract(tmp_path):
    a = sample.generate(tmp_path / "a", orders_per_day=2, seed=7)
    b = sample.generate(tmp_path / "b", orders_per_day=2, seed=7)
    assert a == b
    for table, (file_name, columns) in FILES.items():
        header = (tmp_path / "a" / file_name).read_text(encoding="utf-8").splitlines()[0]
        assert header.split(",") == columns, table
    orders_a = (tmp_path / "a" / FILES["orders"][0]).read_bytes()
    orders_b = (tmp_path / "b" / FILES["orders"][0]).read_bytes()
    assert orders_a == orders_b
