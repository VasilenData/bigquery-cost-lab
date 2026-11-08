import json
import pathlib

import pytest

from bqcost.estimate import MIB, UnsupportedQuery, column_bytes, estimate_query

CAT = json.loads((pathlib.Path(__file__).parents[1] / "catalog" / "lab_tables.json").read_text())


def mib(sql):
    return estimate_query(sql, CAT).processed_bytes / MIB


def test_documented_type_sizes():
    assert column_bytes({"type": "INT64"}) == 8
    assert column_bytes({"type": "NUMERIC"}) == 16
    assert column_bytes({"type": "STRING", "avg_len": 64}) == 66
    assert column_bytes({"type": "DATE"}) == 4


def test_matches_measured_two_column_scan():
    # measured in the BigQuery sandbox: 220.01 MiB for fee (NUMERIC) + block_number (INT64)
    sql = "SELECT SUM(fee) FROM tx_plain WHERE block_number BETWEEN 1 AND 2"
    assert mib(sql) == pytest.approx(220.01, abs=0.01)


def test_predicted_before_measuring_hash_only():
    assert mib("SELECT `hash` FROM tx_plain") == pytest.approx(605.02, abs=0.01)


def test_predicted_before_measuring_select_star():
    assert mib("SELECT * FROM tx_plain") == pytest.approx(1347.55, abs=0.01)


def test_select_star_except_drops_columns():
    full = mib("SELECT * FROM tx_plain")
    less = mib("SELECT * EXCEPT(hash) FROM tx_plain")
    assert less < full
    assert full - less == pytest.approx(605.02, abs=0.01)


def test_limit_does_not_change_estimate():
    assert mib("SELECT `hash` FROM tx_plain LIMIT 10") == mib("SELECT `hash` FROM tx_plain")


def test_count_star_reads_nothing():
    e = estimate_query("SELECT COUNT(*) FROM tx_plain", CAT)
    assert e.processed_bytes == 0 and e.billed_bytes == 0


def test_billing_rounds_up_and_has_ten_mib_floor():
    small = {"t": {"rows": 1000, "columns": {"a": {"type": "INT64"}}}}
    assert estimate_query("SELECT a FROM t", small).billed_bytes == 10 * MIB
    e = estimate_query("SELECT fee, block_number FROM tx_plain", CAT)
    assert e.billed_bytes == 221 * MIB          # 220.01 MiB processed, billed 221 as measured


def test_cost_uses_list_price():
    e = estimate_query("SELECT * FROM tx_plain", CAT)
    assert e.cost_usd == pytest.approx(e.billed_bytes / 2**40 * 6.25)


def test_alias_and_qualified_columns():
    assert mib("SELECT t.fee FROM tx_plain AS t") == pytest.approx(9612337 * 16 / MIB)


def test_unknown_table_is_refused_not_guessed():
    with pytest.raises(UnsupportedQuery):
        estimate_query("SELECT a FROM nope", CAT)
