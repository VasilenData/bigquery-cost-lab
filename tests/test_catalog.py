import json
import pathlib

import pytest

from bqcost.catalog import CatalogError, base_type, build_catalog, main, partition_map
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
            "clustering_ordinal_position": (
                e["cluster_columns"].index(c) + 1 if c in e.get("cluster_columns", []) else None
            ),
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


PARTS = [
    {"table_name": "t", "partition_id": "20240101", "total_rows": "30"},
    {"table_name": "t", "partition_id": "20240102", "total_rows": 70},
]


def test_partition_map_reads_csv_style_strings_and_ints():
    assert partition_map(PARTS) == {"t": {"20240101": 30, "20240102": 70}}


def test_build_catalog_attaches_partitions_only_when_given():
    assert "partitions" not in build_catalog(COLS, {"t": 100})["t"]
    cat = build_catalog(COLS, {"t": 100}, partitions=partition_map(PARTS))
    assert cat["t"]["partitions"] == {"20240101": 30, "20240102": 70}


def test_partition_errors():
    with pytest.raises(CatalogError, match="duplicate partition t.20240101"):
        partition_map(PARTS[:1] + PARTS[:1])
    with pytest.raises(CatalogError, match="missing partition_id"):
        partition_map([{"table_name": "t", "total_rows": 1}])
    with pytest.raises(CatalogError, match="empty partition_id"):
        partition_map([{"table_name": "t", "partition_id": "", "total_rows": 1}])
    with pytest.raises(CatalogError, match="empty partition_id"):
        partition_map([{"table_name": "t", "partition_id": None, "total_rows": 1}])
    with pytest.raises(CatalogError, match="not an integer"):
        partition_map([{"table_name": "t", "partition_id": "20240101", "total_rows": "x"}])
    with pytest.raises(CatalogError, match="negative"):
        partition_map([{"table_name": "t", "partition_id": "20240101", "total_rows": -1}])
    with pytest.raises(CatalogError, match="unknown table nope"):
        build_catalog(COLS, {"t": 1}, partitions={"nope": {"20240101": 1}})
    with pytest.raises(CatalogError, match="no partitioning column"):
        build_catalog(COLS[:1], {"t": 1}, partitions={"t": {"20240101": 1}})


def test_partition_export_feeds_pruning_end_to_end(tmp_path, capsys):
    cols = tmp_path / "cols.csv"
    cols.write_text("table_name,column_name,data_type,is_partitioning_column\nt,id,INT64,NO\nt,d,DATE,YES\n")
    parts = tmp_path / "parts.csv"
    parts.write_text("table_name,partition_id,total_rows\nt,20240101,30\nt,20240102,70\n")
    out = tmp_path / "cat.json"
    assert main([str(cols), "--rows", "t=100", "--partitions", str(parts), "-o", str(out)]) == 0
    cat = json.loads(out.read_text())
    full = estimate_query("SELECT id FROM t", cat)
    one = estimate_query("SELECT id FROM t WHERE d = '2024-01-01'", cat)
    assert full.processed_bytes == 800  # 100 rows x 8 bytes
    assert one.processed_bytes == 360  # 30 rows x (id 8 + d 4) bytes
    assert one.partitions_read == {"t": (1, 2)}


def test_cli_partition_error_exit_code(tmp_path, capsys):
    cols = tmp_path / "cols.csv"
    cols.write_text("table_name,column_name,data_type,is_partitioning_column\nt,id,INT64,NO\n")
    parts = tmp_path / "parts.csv"
    parts.write_text("table_name,partition_id,total_rows\nt,20240101,5\n")
    assert main([str(cols), "--rows", "t=5", "--partitions", str(parts)]) == 2
    assert "no partitioning column" in capsys.readouterr().err


def test_partition_id_whitespace_and_numeric_ids_are_normalized():
    rows = [{"table_name": "t", "partition_id": " 20240101 ", "total_rows": 1}]
    assert partition_map(rows) == {"t": {"20240101": 1}}
    rows = [{"table_name": "t", "partition_id": 20240102, "total_rows": 1}]
    assert partition_map(rows) == {"t": {"20240102": 1}}


# --- clustering_ordinal_position -> cluster_columns ---

def _cols(*positions):
    names = ["id", "ts", "fee", "h"]
    return [{"table_name": "t", "column_name": n, "data_type": "INT64", "clustering_ordinal_position": p}
            for n, p in zip(names, positions, strict=True)]


def test_cluster_columns_are_ordered_by_position_not_row_order():
    cat = build_catalog(_cols("2", "", "1", None), {"t": 5})
    assert cat["t"]["cluster_columns"] == ["fee", "id"]


@pytest.mark.parametrize("empty", ["", None, "NULL", "null", " "])
def test_unclustered_table_has_no_cluster_key(empty):
    cat = build_catalog(_cols(empty, empty, empty, empty), {"t": 5})
    assert "cluster_columns" not in cat["t"]


def test_export_without_the_column_still_works():
    assert "cluster_columns" not in build_catalog(COLS, {"t": 5, }, {"t.h": 8})["t"]


@pytest.mark.parametrize("bad", ["x", "1.5", "0", "-1"])
def test_bad_cluster_position_is_refused(bad):
    with pytest.raises(CatalogError, match="clustering_ordinal_position"):
        build_catalog(_cols(bad, None, None, None), {"t": 5})


def test_duplicate_cluster_position_is_refused():
    with pytest.raises(CatalogError, match="share clustering position 1"):
        build_catalog(_cols("1", "1", None, None), {"t": 5})


def test_cluster_columns_do_not_change_the_estimate():
    plain = build_catalog(_cols(None, None, None, None), {"t": 1000})
    clustered = build_catalog(_cols("1", None, None, None), {"t": 1000})
    sql = "SELECT SUM(id) FROM t WHERE id = 3"
    assert estimate_query(sql, plain).processed_bytes == estimate_query(sql, clustered).processed_bytes


def test_cli_reads_cluster_positions_from_csv(tmp_path):
    src = tmp_path / "c.csv"
    src.write_text("table_name,column_name,data_type,clustering_ordinal_position\n"
                   "t,a,INT64,2\nt,b,INT64,1\nt,c,INT64,\n")
    out = tmp_path / "o.json"
    assert main([str(src), "--rows", "t=3", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["t"]["cluster_columns"] == ["b", "a"]
