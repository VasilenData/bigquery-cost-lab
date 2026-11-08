import argparse
import json
import sys

from .estimate import UnsupportedQuery, estimate_query
from .lint import lint_sql


def main() -> int:
    ap = argparse.ArgumentParser(prog="bqcost", description="Lint and estimate BigQuery query cost")
    ap.add_argument("sql")
    ap.add_argument("--catalog", default="catalog/lab_tables.json")
    args = ap.parse_args()
    with open(args.catalog) as f:
        cat = json.load(f)
    parts = {k: v["partition_column"] for k, v in cat.items() if v.get("partition_column")}
    for f in lint_sql(args.sql, parts):
        print(f"[{f.severity}] {f.rule}: {f.message}")
    try:
        e = estimate_query(args.sql, cat)
        proc, billed = e.processed_bytes / 2**20, e.billed_bytes / 2**20
        print(f"estimate: {proc:.2f} MiB processed, {billed:.0f} MiB billed, "
              f"about ${e.cost_usd:.6f} (upper bound if filtered)")
    except UnsupportedQuery as err:
        print(f"estimate unavailable: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
