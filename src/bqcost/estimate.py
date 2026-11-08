"""Predict bytes scanned from table metadata, using BigQuery's documented per-type sizes.

Scope, stated plainly: this models column pruning only (which columns a query reads). It does not model
partition or cluster pruning, so for filtered queries the result is an upper bound.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

PRICE_PER_TIB_USD = 6.25          # on-demand list price, https://cloud.google.com/bigquery/pricing
MIB = 2**20
MIN_BILLED_PER_TABLE = 10 * MIB   # documented minimum per table referenced

FIXED_BYTES = {"INT64": 8, "FLOAT64": 8, "BOOL": 1, "TIMESTAMP": 8, "DATE": 4, "DATETIME": 8,
               "TIME": 8, "NUMERIC": 16, "BIGNUMERIC": 32}


class UnsupportedQuery(ValueError):
    """Raised when the estimator cannot model a query honestly."""


def column_bytes(col: dict) -> float:
    """Average stored bytes per row for one column description {'type': ..., 'avg_len': ...}."""
    t = col["type"].upper()
    if t in FIXED_BYTES:
        return FIXED_BYTES[t]
    if t == "STRING":
        return 2 + col.get("avg_len", 0)          # documented: 2 bytes + UTF-8 length
    if t == "BYTES":
        return col.get("avg_len", 0)
    raise UnsupportedQuery(f"type {t} is not modeled")


@dataclass
class Estimate:
    processed_bytes: int
    billed_bytes: int
    cost_usd: float
    columns_read: dict[str, list[str]] = field(default_factory=dict)


def _tables_in(tree: exp.Expression) -> dict[str, str]:
    """alias-or-name -> table name for every table in the query."""
    out = {}
    for t in tree.find_all(exp.Table):
        out[(t.alias or t.name).lower()] = t.name.lower()
    return out


def _columns_for(tree: exp.Expression, name: str, alias: str, cols: list[str], single: bool) -> list[str]:
    """Columns of one table that the query reads."""
    read: set[str] = set()
    for star in tree.find_all(exp.Star):
        if isinstance(star.parent, exp.Count):
            continue                                # COUNT(*) reads no column data
        dropped = {c.name.lower() for c in (star.args.get("except_") or star.args.get("except") or [])}
        read |= {c for c in cols if c not in dropped}
    for c in tree.find_all(exp.Column):
        if isinstance(c.this, exp.Star) or isinstance(c.parent, exp.Star):
            continue                                # `*` itself, or a name inside EXCEPT(...)
        qual = (c.table or "").lower()
        cname = c.name.lower()
        belongs = qual in (alias, name) or (not qual and (single or cname in cols))
        if belongs and cname in cols:
            read.add(cname)
    return sorted(read)


def estimate_query(sql: str, catalog: dict) -> Estimate:
    """Estimate processed and billed bytes and on-demand cost for a query over cataloged tables."""
    tree = sqlglot.parse_one(sql, read="bigquery")
    tables = _tables_in(tree)
    if not tables:
        raise UnsupportedQuery("no table referenced")
    processed = billed = 0
    columns_read: dict[str, list[str]] = {}
    for alias, name in tables.items():
        if name not in catalog:
            raise UnsupportedQuery(f"table {name} is not in the catalog")
        meta = catalog[name]
        cols = {k.lower(): v for k, v in meta["columns"].items()}
        read = _columns_for(tree, name, alias, list(cols), single=len(tables) == 1)
        columns_read[name] = read
        nbytes = int(sum(column_bytes(cols[c]) for c in read) * meta["rows"])
        processed += nbytes
        if nbytes:
            billed += max(math.ceil(nbytes / MIB) * MIB, MIN_BILLED_PER_TABLE)
    return Estimate(processed, billed, billed / 2**40 * PRICE_PER_TIB_USD, columns_read)
