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
    if t in {"STRING", "BYTES"}:
        length = col.get("avg_len")
        if (isinstance(length, bool) or not isinstance(length, (int, float))
                or not math.isfinite(length) or length < 0):
            raise UnsupportedQuery(f"{t} requires a finite non-negative avg_len")
        return (2 if t == "STRING" else 0) + length
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
        alias = (t.alias or t.name).lower()
        name = ".".join(part.name for part in t.parts).lower()
        if alias in out or name in out.values():
            raise UnsupportedQuery("duplicate aliases and self-joins are not modeled")
        out[alias] = name
    return out


def _columns_read(tree: exp.Select, schemas: dict[str, dict]) -> dict[str, set[str]]:
    """Resolve the supported single query scope; never silently ignore unknown columns."""
    read = {alias: set() for alias in schemas}
    for star in tree.find_all(exp.Star):
        if isinstance(star.parent, exp.Count):
            continue
        if star.args.get("replace") or star.args.get("rename"):
            raise UnsupportedQuery("SELECT * REPLACE/RENAME is not modeled")
        qualifier = star.parent.table.lower() if isinstance(star.parent, exp.Column) else ""
        if qualifier and qualifier not in schemas:
            raise UnsupportedQuery(f"unknown table alias {qualifier}")
        targets = [qualifier] if qualifier else list(schemas)
        dropped = {c.name.lower() for c in (star.args.get("except_") or star.args.get("except") or [])}
        if dropped - set().union(*(set(schemas[alias]) for alias in targets)):
            raise UnsupportedQuery("EXCEPT references an unknown column")
        for alias in targets:
            read[alias].update(set(schemas[alias]) - dropped)
    projection_aliases = {item.alias.lower() for item in tree.expressions if item.alias}
    for c in tree.find_all(exp.Column):
        if isinstance(c.this, exp.Star) or isinstance(c.parent, exp.Star):
            continue
        qualifier = c.table.lower()
        cname = c.name.lower()
        if qualifier:
            if qualifier not in schemas or cname not in schemas[qualifier]:
                raise UnsupportedQuery(f"unknown column {c.sql(dialect='bigquery')}")
            targets = [qualifier]
        else:
            targets = [alias for alias, columns in schemas.items() if cname in columns]
            post_projection = c.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify)
            if not targets and cname in projection_aliases and post_projection:
                continue  # An output alias in a post-projection clause: its inputs are already visited.
            if len(targets) != 1:
                raise UnsupportedQuery(f"column {cname} is unknown or ambiguous; qualify it explicitly")
        read[targets[0]].add(cname)
    return read


def estimate_query(sql: str, catalog: dict) -> Estimate:
    """Estimate processed and billed bytes and on-demand cost for a query over cataloged tables."""
    statements = sqlglot.parse(sql, read="bigquery")
    if len(statements) != 1:
        raise UnsupportedQuery("estimate one SELECT statement at a time")
    tree = statements[0]
    if not isinstance(tree, exp.Select) or tree.find(exp.CTE) or tree.find(exp.Subquery):
        raise UnsupportedQuery("only a single SELECT scope is modeled; "
                               "CTEs, subqueries and set operations are unsupported")
    if any(join.args.get("using") or join.args.get("method") == "NATURAL"
           for join in tree.find_all(exp.Join)):
        raise UnsupportedQuery("use explicit JOIN ON columns; USING and NATURAL joins are not modeled")
    tables = _tables_in(tree)
    if not tables:
        raise UnsupportedQuery("no table referenced")
    processed = billed = 0
    columns_read: dict[str, list[str]] = {}
    schemas = {}
    for alias, name in tables.items():
        if name not in catalog:
            raise UnsupportedQuery(f"table {name} is not in the catalog")
        schemas[alias] = {k.lower(): v for k, v in catalog[name]["columns"].items()}
    resolved = _columns_read(tree, schemas)
    for alias, name in tables.items():
        meta = catalog[name]
        rows = meta["rows"]
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise UnsupportedQuery(f"table {name} requires a non-negative integer row count")
        cols = schemas[alias]
        read = sorted(resolved[alias])
        columns_read[name] = read
        nbytes = math.ceil(sum(column_bytes(cols[c]) for c in read) * rows)
        processed += nbytes
        if nbytes:
            billed += max(math.ceil(nbytes / MIB) * MIB, MIN_BILLED_PER_TABLE)
    return Estimate(processed, billed, billed / 2**40 * PRICE_PER_TIB_USD, columns_read)
