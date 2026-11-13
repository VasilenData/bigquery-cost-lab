import copy
import json
import pathlib

import pytest

from bqcost.estimate import MIB, estimate_query

LAB = json.loads((pathlib.Path(__file__).parents[1] / "catalog" / "lab_tables.json").read_text())

# Five daily partitions with unequal row counts so a wrong partition set changes the answer.
PARTS = {"20200101": 100_000, "20200102": 200_000, "20200103": 400_000, "20200104": 800_000,
         "20200105": 1_600_000}
ROWS = sum(PARTS.values())


def catalog(col_type="TIMESTAMP", parts=None, rows=ROWS):
    cat = copy.deepcopy(LAB)
    meta = cat["tx_part_clust"]
    meta["rows"] = rows
    meta["columns"]["block_timestamp"]["type"] = col_type
    meta["partitions"] = PARTS if parts is None else parts
    return cat


def run(where, cat=None, select="SELECT SUM(fee), MIN(block_timestamp)", table="tx_part_clust"):
    e = estimate_query(f"{select} FROM {table} WHERE {where}", cat or catalog())
    return e, e.processed_bytes // 24  # fee NUMERIC (16) + block_timestamp TIMESTAMP (8) = 24 bytes/row


@pytest.mark.parametrize("where, expected_rows, kept", [
    ("DATE(block_timestamp) = '2020-01-03'", 400_000, 1),
    ("DATE(block_timestamp) = DATE '2020-01-03'", 400_000, 1),
    ("'2020-01-03' = DATE(block_timestamp)", 400_000, 1),
    ("block_timestamp >= '2020-01-04'", 2_400_000, 2),
    ("'2020-01-04' <= block_timestamp", 2_400_000, 2),
    ("'2020-01-03' > block_timestamp", 300_000, 2),
    ("block_timestamp < '2020-01-03'", 300_000, 2),
    ("block_timestamp BETWEEN '2020-01-02' AND '2020-01-03'", 600_000, 2),
    ("DATE(block_timestamp) BETWEEN '2020-01-02' AND '2020-01-03'", 600_000, 2),
    ("DATE(block_timestamp) > '2020-01-03' AND DATE(block_timestamp) <= '2020-01-04'", 800_000, 1),
    ("block_timestamp >= '2020-01-02' AND fee > 0 AND block_timestamp < '2020-01-04'", 600_000, 2),
])
def test_pruned_rows(where, expected_rows, kept):
    e, rows = run(where)
    assert e.partitions_read == {"tx_part_clust": (kept, 5)}
    assert rows == expected_rows


def test_timestamp_greater_than_midnight_keeps_that_day():
    # rows later on 2020-01-03 are > midnight, so the day cannot be dropped for a TIMESTAMP column
    e, _ = run("block_timestamp > '2020-01-03'")
    assert e.partitions_read["tx_part_clust"] == (3, 5)


def test_date_column_greater_than_drops_that_day():
    e, _ = run("block_timestamp > '2020-01-03'", catalog("DATE"))
    assert e.partitions_read["tx_part_clust"] == (2, 5)
    assert e.processed_bytes == 2_400_000 * (16 + 4)  # a DATE column is 4 bytes, not 8


def test_exact_bytes_for_one_day():
    e, _ = run("DATE(block_timestamp) = '2020-01-03'")
    assert e.processed_bytes == 400_000 * 24


def test_no_matching_partition_scans_nothing():
    e, _ = run("DATE(block_timestamp) = '2021-06-01'")
    assert e.partitions_read["tx_part_clust"] == (0, 5)
    assert e.processed_bytes == 0 and e.billed_bytes == 0


@pytest.mark.parametrize("where", [
    "DATE(block_timestamp) = '2020-01-03' OR fee > 0",   # OR at top level
    "fee > 0",                                           # not the partition column
    "EXTRACT(YEAR FROM block_timestamp) = 2020",         # function that is not DATE()
    "DATE(block_timestamp) = '2020-13-45'",              # not a real date
    "DATE(block_timestamp) = '2020-01-03' OR DATE(block_timestamp) = '2020-01-04'",
    "NOT DATE(block_timestamp) = '2020-01-03'",
    "block_timestamp >= TIMESTAMP '2020-01-04 12:00:00'",
])
def test_unsupported_predicates_keep_everything(where):
    e, rows = run(where)
    assert e.partitions_read == {}
    assert rows == ROWS


def test_no_where_clause_and_no_partition_metadata_are_unchanged():
    plain = estimate_query("SELECT SUM(fee) FROM tx_part_clust", catalog())
    assert plain.processed_bytes == ROWS * 16 and plain.partitions_read == {}
    e = estimate_query("SELECT SUM(fee) FROM tx_part_clust WHERE DATE(block_timestamp) = '2020-01-03'", LAB)
    assert e.processed_bytes == LAB["tx_part_clust"]["rows"] * 24 and e.partitions_read == {}


def test_special_or_hourly_partition_ids_disable_pruning():
    for pid in ("__NULL__", "2020010312", "202001"):
        parts = dict(PARTS, **{pid: 5})
        e, rows = run("DATE(block_timestamp) = '2020-01-03'", catalog(parts=parts, rows=ROWS + 5))
        assert e.partitions_read == {}
        assert rows == ROWS + 5


def test_non_time_partition_column_type_is_ignored():
    e, _ = run("DATE(block_timestamp) = '2020-01-03'", catalog("INT64"))
    assert e.partitions_read == {}


def test_qualified_and_aliased_column():
    sql = "SELECT SUM(t.fee) FROM tx_part_clust AS t WHERE DATE(t.block_timestamp) = '2020-01-03'"
    e = estimate_query(sql, catalog())
    assert e.partitions_read == {"tx_part_clust": (1, 5)}
    assert e.processed_bytes == 400_000 * 24


def test_joins_are_not_pruned():
    e = estimate_query("SELECT SUM(a.fee) FROM tx_part_clust a JOIN tx_plain b ON a.hash = b.hash "
                       "WHERE DATE(a.block_timestamp) = '2020-01-03'", catalog())
    assert e.partitions_read == {}


def test_billing_minimum_still_applies_to_pruned_scan():
    parts = {"20200101": 1000, "20200102": 9_999_000}
    e, _ = run("DATE(block_timestamp) = '2020-01-01'", catalog(parts=parts, rows=10_000_000))
    assert e.processed_bytes == 24_000 and e.billed_bytes == 10 * MIB


def test_lab_numbers_stay_unchanged_without_partition_listing():
    e = estimate_query("SELECT SUM(fee) FROM tx_plain WHERE block_number BETWEEN 1 AND 2", LAB)
    assert e.processed_bytes / MIB == pytest.approx(220.01, abs=0.01)
