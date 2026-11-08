"""Rule-based linter for BigQuery SQL cost anti-patterns. Each rule cites a measurement in the README."""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str      # "high" | "medium" | "info"
    message: str


def _real_stars(tree):
    return [s for s in tree.find_all(exp.Star) if not isinstance(s.parent, exp.Count)]


def _filters_on(tree, column: str, alias: str, single: bool) -> bool:
    where = tree.args.get("where")
    if where is None:
        return False
    return any(c.name.lower() == column.lower() and
               (c.table.lower() == alias or (not c.table and single)) for c in where.find_all(exp.Column))


def lint_sql(sql: str, partition_columns: dict[str, str] | None = None) -> list[Finding]:
    """Return findings for one statement. partition_columns maps table name -> partition column."""
    tree = sqlglot.parse_one(sql, read="bigquery")
    out: list[Finding] = []
    stars = _real_stars(tree)
    if stars:
        out.append(Finding("BQ001", "high", "SELECT * reads every column; list the columns you need "
                           "(or use SELECT * EXCEPT(...) to drop wide nested ones)."))
    has_limit = tree.find(exp.Limit) is not None
    has_filter = tree.find(exp.Where) is not None
    has_agg = tree.find(exp.AggFunc) is not None or tree.find(exp.Group) is not None
    if has_limit and not has_filter and not has_agg:
        out.append(Finding("BQ002", "high", "LIMIT does not reduce bytes scanned. Filter on a partition "
                           "column or sample the table instead."))
    tables = list(tree.find_all(exp.Table))
    for table in tables:
        name, alias = table.name.lower(), (table.alias or table.name).lower()
        col = (partition_columns or {}).get(name)
        if col and not _filters_on(tree, col, alias, single=len(tables) == 1):
            out.append(Finding("BQ003", "high",
                               f"No matching WHERE filter on partition column {alias}.{col}; "
                               "check the dry-run estimate before running this query."))
    if any(isinstance(c.this, exp.Distinct) for c in tree.find_all(exp.Count)):
        out.append(Finding("BQ004", "info", "COUNT(DISTINCT x) reads the same bytes as "
                           "APPROX_COUNT_DISTINCT(x); the approximate form saves compute, not scan cost."))
    return out
