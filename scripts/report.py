"""Turn measurements/public_dryruns.json into a cost table and chart. Console units are binary."""
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

UNITS = {"TB": 2**40, "GB": 2**30, "MB": 2**20, "B": 1}
PRICE = 6.25
BG, INK, MUTED, MINT, AMBER = "#111315", "#ECEFF1", "#A3ACB3", "#7EE0B8", "#F0B86E"
LABELS = {
    "select_star_limit": "SELECT * ... LIMIT 10",
    "star_except_nested": "SELECT * EXCEPT(inputs, outputs) ... LIMIT 10",
    "two_columns": "two columns, no filter",
    "one_column_limit": "one column ... LIMIT 10",
    "count_distinct": "COUNT(DISTINCT hash)",
    "approx_count_distinct": "APPROX_COUNT_DISTINCT(hash)",
    "non_partition_filter": "one column, filter block_number > 600000",
    "extract_year_filter": "one column, filter EXTRACT(YEAR ...) = 2020",
    "partition_month_filter": "one column, filter block_timestamp_month = Jan 2020",
    "date_wrapped_filter": "one column, filter DATE(block_timestamp) = one day",
    "count_star": "COUNT(*)",
}


def to_bytes(text: str) -> float:
    n, unit = text.split()
    return float(n) * UNITS[unit]


def main() -> None:
    with open("measurements/public_dryruns.json") as f:
        data = json.load(f)
    rows = [(q["id"], q["sql"], to_bytes(q["bytes_text"])) for q in data["queries"]]
    rows.sort(key=lambda r: -r[2])
    head = "| Query (table t = crypto_bitcoin.transactions) | Console estimate | On-demand cost |"
    lines = [head, "|---|---|---|"]
    for qid, _sql, b in rows:
        cost = b / 2**40 * PRICE
        text = next(q["bytes_text"] for q in data["queries"] if q["id"] == qid)
        lines.append(f"| {LABELS[qid]} | {text} | ${cost:,.2f} |" if cost >= 0.01 else
                     f"| {LABELS[qid]} | {text} | under $0.01 |")
    with open("reports/public_costs.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    shown = [r for r in rows if r[2] > 0]
    fig, ax = plt.subplots(figsize=(10, 4.6), facecolor=BG)
    ax.set_facecolor(BG)
    ys = range(len(shown))
    cols = [AMBER if r[0] in ("select_star_limit", "star_except_nested") else MINT for r in shown]
    ax.barh(list(ys), [r[2] / 2**20 for r in shown], color=cols)
    ax.set_xscale("log")
    ax.set_yticks(list(ys))
    ax.set_yticklabels([LABELS[r[0]] for r in shown], color=INK, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("MiB scanned (log scale), dry-run estimate", color=MUTED)
    ax.tick_params(colors=MUTED)
    for s in ax.spines.values():
        s.set_color("#2B3035")
    for y, r in zip(ys, shown, strict=True):
        ax.text(r[2] / 2**20 * 1.15, y, f"${r[2] / 2**40 * PRICE:,.2f}" if r[2] / 2**40 * PRICE >= 0.01
                else "<$0.01", va="center", color=INK, fontsize=8)
    ax.set_xlim(right=2**20 * 20)
    plt.tight_layout()
    plt.savefig("reports/public_dryruns.png", dpi=150, facecolor=BG)


if __name__ == "__main__":
    main()
