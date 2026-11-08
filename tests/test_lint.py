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
