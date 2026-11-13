"""Partition pruning from per-partition row counts (INFORMATION_SCHEMA.PARTITIONS).

Scope, stated plainly: daily partitions only, a single-table query, and a WHERE clause whose top level is
AND of simple comparisons between the partition column (or DATE(partition column)) and a date literal.
Anything else keeps every partition, so the estimate stays an upper bound. Clustering is not modeled.
"""
from __future__ import annotations

import datetime as dt

from sqlglot import exp

_FLIP = {exp.LT: exp.GT, exp.LTE: exp.GTE, exp.GT: exp.LT, exp.GTE: exp.LTE, exp.EQ: exp.EQ}


def _parse_day_id(partition_id: str) -> dt.date | None:
    if len(partition_id) == 8 and partition_id.isdigit():
        try:
            return dt.datetime.strptime(partition_id, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _literal_date(node: exp.Expression) -> dt.date | None:
    """A date literal: '2020-01-15' or DATE '2020-01-15' (also DATE('2020-01-15'))."""
    target = node.args.get("to") if isinstance(node, exp.Cast) else None
    is_cast = target is not None and target.is_type("date")
    if is_cast or (isinstance(node, exp.Date) and not node.expressions):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        try:
            return dt.date.fromisoformat(node.name)
        except ValueError:
            return None
    return None


def _column_side(node: exp.Expression, column: str, alias: str, name: str):
    """Return 'ts' for the bare column, 'date' for DATE(column), or None if not the partition column."""
    inner, kind = node, "ts"
    if isinstance(node, exp.Date) and not node.expressions:
        inner, kind = node.this, "date"
    if isinstance(inner, exp.Column) and inner.name.lower() == column.lower() \
            and (not inner.table or inner.table.lower() in {alias, name}):
        return kind
    return None


def _conjuncts(where: exp.Expression) -> list[exp.Expression]:
    node = where.this if isinstance(where, exp.Where) else where
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _bounds(node: exp.Expression, column: str, alias: str, name: str, col_type: str):
    """Translate one conjunct into a list of (op, date) constraints, or [] if not understood."""
    if isinstance(node, exp.Between):
        lo, hi = _literal_date(node.args["low"]), _literal_date(node.args["high"])
        kind = _column_side(node.this, column, alias, name)
        if kind and lo and hi:
            return [(kind, ">=", lo), (kind, "<=", hi)]
        return []
    for cls in _FLIP:
        if isinstance(node, cls):
            op = {exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">=", exp.EQ: "="}[cls]
            kind, lit = _column_side(node.left, column, alias, name), _literal_date(node.right)
            if kind is None or lit is None:
                kind, lit = _column_side(node.right, column, alias, name), _literal_date(node.left)
                op = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}[op]
            if kind and lit:
                return [(kind, op, lit)]
    return []


def _day_may_match(day: dt.date, kind: str, op: str, lit: dt.date, col_type: str) -> bool:
    """Could any row of the daily partition `day` satisfy `column op lit`? Never says no when unsure."""
    # A TIMESTAMP/DATETIME value in the day lies in [day, day+1); DATE(col) and DATE columns equal the day.
    point = kind == "date" or col_type == "DATE"
    if op == "=":
        return day == lit
    if op == ">":
        return day > lit if point else day >= lit
    if op == ">=":
        return day >= lit
    if op == "<":
        return day < lit
    return day <= lit  # "<="


def prune_partitions(tree: exp.Select, name: str, alias: str, meta: dict) -> tuple[int, int, int] | None:
    """Return (rows_kept, partitions_kept, partitions_total) or None when no pruning applies."""
    column, parts = meta.get("partition_column"), meta.get("partitions")
    where = tree.args.get("where")
    if not column or not parts or where is None:
        return None
    col_type = meta["columns"].get(column, {}).get("type", "").upper()
    if col_type not in {"DATE", "TIMESTAMP", "DATETIME"}:
        return None
    days = {pid: _parse_day_id(pid) for pid in parts}
    if any(d is None for d in days.values()):
        return None  # hourly, monthly, integer-range or special partitions: keep everything
    constraints = [b for c in _conjuncts(where) for b in _bounds(c, column, alias, name, col_type)]
    if not constraints:
        return None
    kept = [pid for pid, day in days.items()
            if all(_day_may_match(day, k, op, lit, col_type) for k, op, lit in constraints)]
    return sum(parts[pid] for pid in kept), len(kept), len(parts)
