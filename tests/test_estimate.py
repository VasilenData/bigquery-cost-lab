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


def test_billing_invariants_across_integer_precision_and_mib_boundaries():
    # Integer catalogs are not limited to the range of exactly representable doubles.
    boundaries = [0, 1, 9, 10, 11, 1000, 2**33, 2**40, 2**43]
    for mib_boundary in boundaries:
        for delta in [-1, 0, 1]:
            rows = max(0, mib_boundary * MIB // 8 + delta)
            catalog = {'t': {'rows': rows, 'columns': {'x': {'type': 'INT64'}}}}
            estimate = estimate_query('SELECT x FROM t', catalog)
            assert estimate.processed_bytes == rows * 8
            assert estimate.billed_bytes >= estimate.processed_bytes
            assert estimate.billed_bytes % MIB == 0
            if estimate.processed_bytes >= 10 * MIB:
                assert estimate.billed_bytes - estimate.processed_bytes < MIB
            elif estimate.processed_bytes:
                assert estimate.billed_bytes == 10 * MIB
            else:
                assert estimate.billed_bytes == 0


def test_alias_and_qualified_columns():
    assert mib("SELECT t.fee FROM tx_plain AS t") == pytest.approx(9612337 * 16 / MIB)


def test_unknown_table_is_refused_not_guessed():
    with pytest.raises(UnsupportedQuery):
        estimate_query("SELECT a FROM nope", CAT)


@pytest.mark.parametrize("sql", ["SELECT typo FROM tx_plain", "SELECT typo AS typo FROM tx_plain",
                                  "SELECT missing.fee FROM tx_plain",
                                  "SELECT * EXCEPT(typo) FROM tx_plain",
                                  "SELECT fee FROM other_dataset.tx_plain"])
def test_unknown_columns_aliases_and_qualified_tables_are_refused(sql):
    with pytest.raises(UnsupportedQuery):
        estimate_query(sql, CAT)


def test_qualified_star_reads_only_its_table_and_join_columns():
    catalog = {
        "left_table": {"rows": 10, "columns": {"id": {"type": "INT64"}, "value": {"type": "NUMERIC"}}},
        "right_table": {"rows": 20, "columns": {
            "id": {"type": "INT64"}, "wide": {"type": "STRING", "avg_len": 1000}}},
    }
    result = estimate_query("SELECT l.* FROM left_table l JOIN right_table r ON l.id = r.id", catalog)
    assert result.processed_bytes == 10 * 24 + 20 * 8
    assert result.columns_read == {"left_table": ["id", "value"], "right_table": ["id"]}
    with pytest.raises(UnsupportedQuery, match="ambiguous"):
        estimate_query("SELECT id FROM left_table l JOIN right_table r ON l.id = r.id", catalog)


@pytest.mark.parametrize("sql", [
    "WITH x AS (SELECT fee FROM tx_plain) SELECT * FROM x",
    "SELECT * FROM (SELECT fee FROM tx_plain)",
    "SELECT fee FROM tx_plain UNION ALL SELECT fee FROM tx_plain",
    "SELECT a.fee FROM tx_plain a JOIN tx_plain b ON a.hash = b.hash",
])
def test_unmodeled_query_scopes_are_rejected(sql):
    with pytest.raises(UnsupportedQuery):
        estimate_query(sql, CAT)


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), True])
def test_unknown_or_invalid_variable_lengths_are_not_assumed_zero(value):
    with pytest.raises(UnsupportedQuery, match="avg_len"):
        column_bytes({"type": "STRING", "avg_len": value})


def test_output_alias_does_not_create_an_extra_scanned_column():
    sql = "SELECT SUM(fee) AS total_fee FROM tx_plain ORDER BY total_fee"
    assert mib(sql) == mib("SELECT SUM(fee) FROM tx_plain")
