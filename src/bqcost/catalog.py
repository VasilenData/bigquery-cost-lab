"""Build a bqcost catalog from INFORMATION_SCHEMA exports instead of writing it by hand.

Input is what BigQuery returns for
    SELECT table_name, column_name, data_type, is_partitioning_column
    FROM `project.dataset`.INFORMATION_SCHEMA.COLUMNS
exported as CSV or JSON (a list of row objects) from the console. Row counts come from
`INFORMATION_SCHEMA.TABLE_STORAGE` (total_rows) or from --rows.

What metadata cannot give you: the average length of STRING and BYTES values. Those columns get
no avg_len unless you pass --avg-len table.column=N, and the estimator refuses to price a query that
reads such a column. A missing number stays missing; it is never guessed.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys


class CatalogError(ValueError):
    """Raised when the exported metadata cannot be turned into a catalog honestly."""


def base_type(data_type: str) -> str:
    """'NUMERIC(10, 2)' -> 'NUMERIC', 'string(10)' -> 'STRING'. Complex types are kept whole."""
    t = data_type.strip().upper()
    if "<" in t:
        return t
    return re.sub(r"\(.*\)$", "", t).strip()


def load_rows(path: str) -> list[dict]:
    with open(path, newline="") as f:
        text = f.read()
    if path.lower().endswith(".json"):
        rows = json.loads(text)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise CatalogError(f"{path}: expected a JSON list of row objects")
        return rows
    return list(csv.DictReader(text.splitlines()))


def build_catalog(
    columns: list[dict], rows: dict[str, int] | None = None, avg_len: dict[str, float] | None = None
) -> dict:
    rows = rows or {}
    avg_len = avg_len or {}
    cat: dict = {}
    for r in columns:
        try:
            table, name, dtype = r["table_name"], r["column_name"], r["data_type"]
        except KeyError as e:
            raise CatalogError(f"column row is missing {e.args[0]}: {r}") from None
        entry = cat.setdefault(table, {"rows": None, "partition_column": None, "columns": {}})
        if name in entry["columns"]:
            raise CatalogError(f"duplicate column {table}.{name}")
        col = {"type": base_type(dtype)}
        if col["type"] in {"STRING", "BYTES"} and f"{table}.{name}" in avg_len:
            col["avg_len"] = avg_len[f"{table}.{name}"]
        entry["columns"][name] = col
        if str(r.get("is_partitioning_column", "NO")).upper() == "YES":
            entry["partition_column"] = name
    for table, n in rows.items():
        if table not in cat:
            raise CatalogError(f"row count given for unknown table {table}")
        cat[table]["rows"] = n
    missing = sorted(t for t, e in cat.items() if e["rows"] is None)
    if missing:
        raise CatalogError("no row count for: " + ", ".join(missing))
    return cat


def _pairs(items: list[str], conv) -> dict:
    out = {}
    for item in items or []:
        key, sep, val = item.rpartition("=")
        if not sep or not key:
            raise CatalogError(f"expected NAME=VALUE, got {item!r}")
        try:
            out[key] = conv(val)
        except ValueError:
            raise CatalogError(f"bad value in {item!r}") from None
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bqcost.catalog", description=__doc__.split("\n")[0])
    ap.add_argument("columns", help="INFORMATION_SCHEMA.COLUMNS export (.csv or .json)")
    ap.add_argument("--storage", help="TABLE_STORAGE export with table_name,total_rows")
    ap.add_argument("--rows", action="append", metavar="TABLE=N", help="row count for a table")
    ap.add_argument(
        "--avg-len", action="append", metavar="TABLE.COLUMN=N", help="average length of a STRING/BYTES column"
    )
    ap.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    args = ap.parse_args(argv)
    try:
        rows = {}
        if args.storage:
            rows.update({r["table_name"]: int(r["total_rows"]) for r in load_rows(args.storage)})
        rows.update(_pairs(args.rows, int))
        cat = build_catalog(load_rows(args.columns), rows, _pairs(args.avg_len, float))
    except (CatalogError, OSError, KeyError, ValueError) as err:
        print(f"catalog error: {err}", file=sys.stderr)
        return 2
    text = json.dumps(cat, indent=2) + "\n"
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
