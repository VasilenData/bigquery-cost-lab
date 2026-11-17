import json

from bqcost.__main__ import main
from bqcost.lint import lint_sql

PARTS = {"tx_part_clust": "block_timestamp"}


def rules(sql, parts=None):
    return {f.rule for f in lint_sql(sql, parts)}


def test_select_star_flagged():
    assert "BQ001" in rules("SELECT * FROM tx_plain")


def test_count_star_is_not_select_star():
    assert "BQ001" not in rules("SELECT COUNT(*) FROM tx_plain")


def test_star_except_still_flagged_as_star():
    assert "BQ001" in rules("SELECT * EXCEPT(a) FROM tx_plain")


def test_limit_without_filter_flagged():
    assert "BQ002" in rules("SELECT hash FROM tx_plain LIMIT 10")


def test_limit_with_filter_not_flagged():
    assert "BQ002" not in rules("SELECT hash FROM tx_plain WHERE block_number > 5 LIMIT 10")


def test_limit_with_aggregate_not_flagged():
    assert "BQ002" not in rules("SELECT MAX(fee) FROM tx_plain LIMIT 1")


def test_missing_partition_filter_flagged():
    assert "BQ003" in rules("SELECT fee FROM tx_part_clust WHERE block_number > 5", PARTS)


def test_partition_filter_present_ok():
    sql = "SELECT fee FROM tx_part_clust WHERE DATE(block_timestamp) = '2020-01-15'"
    assert "BQ003" not in rules(sql, PARTS)


def test_no_partition_rule_without_config():
    assert "BQ003" not in rules("SELECT fee FROM tx_part_clust")


def test_count_distinct_is_info_only():
    f = [x for x in lint_sql("SELECT COUNT(DISTINCT hash) FROM tx_plain") if x.rule == "BQ004"]
    assert f and f[0].severity == "info"


def test_clean_query_has_no_findings():
    sql = "SELECT fee FROM tx_part_clust WHERE DATE(block_timestamp) = '2020-01-15'"
    assert lint_sql(sql, PARTS) == []


def test_filter_on_other_joined_table_does_not_hide_missing_partition_filter():
    sql = ("SELECT p.fee FROM tx_part_clust p JOIN tx_plain q ON p.hash=q.hash "
           "WHERE q.block_timestamp > '2020-01-01'")
    assert "BQ003" in rules(sql, PARTS)
    assert "BQ003" not in rules(sql.replace("WHERE q.", "WHERE p."), PARTS)


def clustered(sql):
    return [f for f in lint_sql(sql, None, {"t": ["a", "b"]}) if f.rule == "BQ005"]


def test_no_filter_on_leading_cluster_column_is_info():
    found = clustered("SELECT SUM(x) FROM t WHERE b = 1")
    assert len(found) == 1 and found[0].severity == "info"
    assert "leading column a" in found[0].message


def test_filter_on_leading_cluster_column_is_quiet():
    assert clustered("SELECT SUM(x) FROM t WHERE a BETWEEN 1 AND 5") == []


def test_no_where_at_all_flags_cluster_rule():
    assert len(clustered("SELECT SUM(x) FROM t")) == 1


def test_cluster_rule_off_without_config_or_for_other_tables():
    assert [f for f in lint_sql("SELECT SUM(x) FROM t") if f.rule == "BQ005"] == []
    assert [f for f in lint_sql("SELECT SUM(x) FROM u", None, {"t": ["a"]}) if f.rule == "BQ005"] == []


def test_filter_on_other_joined_table_does_not_hide_missing_cluster_filter():
    found = clustered("SELECT SUM(t.x) FROM t JOIN u ON t.id = u.id WHERE u.a = 1")
    assert len(found) == 1


def test_qualified_filter_on_cluster_column_counts():
    assert clustered("SELECT SUM(t.x) FROM t JOIN u ON t.id = u.id WHERE t.a = 1") == []


def test_cli_passes_catalog_cluster_columns_to_the_linter(tmp_path, monkeypatch, capsys):
    cat = {"t": {"rows": 10, "partition_column": None, "cluster_columns": ["a"],
                 "columns": {"a": {"type": "INT64"}, "x": {"type": "INT64"}}}}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cat))
    monkeypatch.setattr("sys.argv", ["bqcost", "SELECT SUM(x) FROM t", "--catalog", str(path)])
    assert main() == 0
    assert "BQ005" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["bqcost", "SELECT SUM(x) FROM t WHERE a = 1", "--catalog", str(path)])
    assert main() == 0
    assert "BQ005" not in capsys.readouterr().out
