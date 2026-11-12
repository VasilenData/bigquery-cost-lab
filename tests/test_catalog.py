import json
import pathlib

import pytest

from bqcost.catalog import CatalogError, base_type, build_catalog, main
from bqcost.estimate import UnsupportedQuery, estimate_query

LAB = json.loads((pathlib.Path(__file__).parents[1] / "catalog" / "lab_tables.json").read_text())

COLS = [
    {"table_name": "t", "column_name": "id", "data_type": "INT64", "is_partitioning_column": "NO"},
    {"table_name": "t", "column_name": "ts", "data_type": "TIMESTAMP", "is_partitioning_column": "YES"},
    {"table_name": "t", "column_name": "fee", "data_type": "NUMERIC(10, 2)", "is_partitioning_column": "NO"},
    {"table_name": "t", "column_name": "h", "data_type": "STRING(64)", "is_partitioning_column": "NO"},
]


def test_base_type_strips_parameters_but_keeps_complex_types():
    assert base_type("NUMERIC(10, 2)") == "NUMERIC"
    assert base_type("string(10)") == "STRING"
    assert base_type(" int64 ") == "INT64"
    assert base_type("ARRAY<STRING(5)>") == "ARRAY<STRING(5)>"


def test_build_catalog_reads_partition_column_and_types():
    cat = build_catalog(COLS, {"t": 100}, {"t.h": 64})
    assert cat["t"]["rows"] == 100
    assert cat["t"]["partition_column"] == "ts"
    assert cat["t"]["columns"]["fee"] == {"type": "NUMERIC"}
    assert cat["t"]["columns"]["h"] == {"type": "STRING", "avg_len": 64}


def test_no_partition_column_is_null():
    cat = build_catalog(COLS[:1], {"t": 1})
    assert cat["t"]["partition_column"] is None


def test_string_without_avg_len_stays_missing_and_estimator_refuses():
    cat = build_catalog(COLS, {"t": 100})
    assert "avg_len" not in cat["t"]["columns"]["h"]
    with pytest.raises(UnsupportedQuery):
        estimate_query("SELECT h FROM t", cat)
    assert estimate_query("SELECT id FROM t", cat).processed_bytes == 800


def test_avg_len_is_ignored_for_non_string_columns():
    cat = build_catalog(COLS, {"t": 1}, {"t.id": 5})
    assert "avg_len" not in cat["t"]["columns"]["id"]


def test_missing_row_count_is_an_error():
    with pytest.raises(CatalogError, match="no row count for: t"):
        build_catalog(COLS)


def test_row_count_for_unknown_table_is_an_error():
    with pytest.raises(CatalogError, match="unknown table nope"):
        build_catalog(COLS, {"t": 1, "nope": 2})


def test_duplicate_and_incomplete_rows_are_errors():
    with pytest.raises(CatalogError, match="duplicate column t.id"):
        build_catalog(COLS[:1] + COLS[:1], {"t": 1})
    with pytest.raises(CatalogError, match="missing data_type"):
        build_catalog([{"table_name": "t", "column_name": "x"}], {"t": 1})


def test_rebuilds_the_hand_written_lab_catalog(tmp_path):
    cols = [
        {
            "table_name": t,
            "column_name": c,
            "data_type": v["type"],
            "is_partitioning_column": "YES" if c == e["partition_column"] else "NO",
        }
        for t, e in LAB.items()
        for c, v in e["columns"].items()
    ]
    (tmp_path / "cols.json").write_text(json.dumps(cols))
    (tmp_path / "storage.csv").write_text(
        "table_name,total_rows\n" + "".join(f"{t},{e['rows']}\n" for t, e in LAB.items())
    )
    out = tmp_path / "cat.json"
    code = main(
        [
            str(tmp_path / "cols.json"),
            "--storage",
            str(tmp_path / "storage.csv"),
            "--avg-len",
            "tx_plain.hash=64",
            "--avg-len",
            "tx_clust.hash=64",
            "--avg-len",
            "tx_part_clust.hash=64",
            "-o",
            str(out),
        ]
    )
    assert code == 0
    assert json.loads(out.read_text()) == LAB


def test_csv_input_and_error_exit_code(tmp_path, capsys):
    p = tmp_path / "c.csv"
    p.write_text("table_name,column_name,data_type,is_partitioning_column\nt,id,INT64,NO\n")
    assert main([str(p), "--rows", "t=5"]) == 0
    assert json.loads(capsys.readouterr().out)["t"]["rows"] == 5
    assert main([str(p)]) == 2
    assert "no row count" in capsys.readouterr().err
    assert main([str(p), "--rows", "bad"]) == 2
    assert "NAME=VALUE" in capsys.readouterr().err
    assert main([str(p), "--rows", "=5"]) == 2
    assert "NAME=VALUE" in capsys.readouterr().err
    assert main([str(p), "--rows", "t=x"]) == 2
    assert "bad value" in capsys.readouterr().err
