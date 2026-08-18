"""Regenerate output/adoption_monitor.xlsx and the PNG exhibits.

Usage (from repo root):
    uv run python -m src.exhibits

Reads the latest snapshot in history/adoption_log.csv (never the raw loan
data — exhibits are always reproducible from the committed log) and writes:

  output/adoption_monitor.xlsx
    Summary — latest origination month, VS4 share by focus lender
    Trend   — VS4 UPB share by month x lender, with a line chart
    Log     — the full append-only history

  output/exhibits/
    vs4_share_aggregate.png  — aggregate VS4 share (loan count vs UPB weighted)
    vs4_share_by_lender.png  — VS4 UPB share by focus lender

Chart colors come from the validated palette in settings.yaml; each focus
lender keeps a fixed color slot (order in focus_lenders), so colors are
stable across months as lenders enter or leave the exhibits.
"""

from __future__ import annotations

import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import PercentFormatter

from src.common import AGGREGATE_LENDER, OTHER_LENDER, load_settings, resolve_path

AGGREGATE_COLOR = "#0b0b0b"
OTHER_COLOR = "#a5a49b"
TEXT_SECONDARY = "#52514e"


def load_latest_snapshot(settings: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (latest snapshot rows, full history log)."""
    history_file = resolve_path(settings, "history_file")
    if not history_file.exists() or history_file.stat().st_size == 0:
        sys.exit(f"{history_file} is empty. Run: uv run python -m src.adoption")
    log = pd.read_csv(history_file, dtype={"snapshot_date": str, "origination_month": str})
    if log.empty:
        sys.exit(f"{history_file} has no rows. Run: uv run python -m src.adoption")
    latest = log["snapshot_date"].max()
    return log[log["snapshot_date"] == latest].copy(), log


def fold_to_focus(snap: pd.DataFrame, focus: list[str]) -> pd.DataFrame:
    """Collapse non-focus lenders into 'Other'; keep the aggregate row apart."""
    df = snap[snap["lender"] != AGGREGATE_LENDER].copy()
    df["lender"] = df["lender"].where(df["lender"].isin(focus), OTHER_LENDER)
    folded = df.groupby(["origination_month", "lender"], as_index=False)[
        ["loans", "loans_vs4", "upb", "upb_vs4"]
    ].sum()
    folded["vs4_share_loans"] = folded["loans_vs4"] / folded["loans"]
    folded["vs4_share_upb"] = folded["upb_vs4"] / folded["upb"].replace(0, pd.NA)
    return folded


def lender_colors(settings: dict) -> dict[str, str]:
    palette = settings["palette"]
    focus = settings["focus_lenders"]
    colors = {name: palette[i % len(palette)] for i, name in enumerate(focus)}
    colors[AGGREGATE_LENDER] = AGGREGATE_COLOR
    colors[OTHER_LENDER] = OTHER_COLOR
    return colors


# ---------------------------------------------------------------- Excel ----

def write_workbook(snap: pd.DataFrame, log: pd.DataFrame, settings: dict) -> None:
    out_path = resolve_path(settings, "output_xlsx")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    focus = settings["focus_lenders"]
    colors = lender_colors(settings)

    folded = fold_to_focus(snap, focus)
    agg = snap[snap["lender"] == AGGREGATE_LENDER]
    latest_month = snap["origination_month"].max()

    lender_order = focus + [OTHER_LENDER]
    summary = folded[folded["origination_month"] == latest_month].set_index("lender")
    summary = summary.reindex([l for l in lender_order if l in summary.index]).reset_index()

    trend = pd.concat([agg, folded], ignore_index=True).pivot_table(
        index="origination_month", columns="lender", values="vs4_share_upb"
    )
    trend = trend[[c for c in [AGGREGATE_LENDER] + lender_order if c in trend.columns]]

    with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
        wb = writer.book
        pct = wb.add_format({"num_format": "0.0%"})
        intfmt = wb.add_format({"num_format": "#,##0"})
        usd_m = wb.add_format({"num_format": '#,##0.0,,"M"'})
        hdr = wb.add_format({"bold": True, "bottom": 1})
        title = wb.add_format({"bold": True, "font_size": 12})

        # Summary sheet
        ws = wb.add_worksheet("Summary")
        writer.sheets["Summary"] = ws
        ws.write(0, 0, f"VS4 adoption — origination month {latest_month} "
                       f"(snapshot {snap['snapshot_date'].iloc[0]})", title)
        cols = ["Lender", "Loans", "VS4 loans", "UPB ($)", "VS4 UPB ($)",
                "VS4 share (loans)", "VS4 share (UPB)"]
        for c, name in enumerate(cols):
            ws.write(2, c, name, hdr)
        table = pd.concat([summary, agg[agg["origination_month"] == latest_month]],
                          ignore_index=True)
        value_cols = ["loans", "loans_vs4", "upb", "upb_vs4",
                      "vs4_share_loans", "vs4_share_upb"]
        formats = [intfmt, intfmt, usd_m, usd_m, pct, pct]
        for r, (_, row) in enumerate(table.iterrows(), start=3):
            ws.write(r, 0, row["lender"])
            for c, (col, fmt) in enumerate(zip(value_cols, formats), start=1):
                ws.write_number(r, c, float(row[col]) if pd.notna(row[col]) else 0, fmt)
        ws.set_column(0, 0, 18)
        ws.set_column(1, 6, 14)

        # Trend sheet + native line chart
        trend.reset_index().to_excel(writer, sheet_name="Trend", index=False, startrow=1)
        ws_t = writer.sheets["Trend"]
        ws_t.write(0, 0, "VS4 share of UPB by origination month", title)
        ws_t.set_column(0, len(trend.columns), 14, None)
        for c in range(1, len(trend.columns) + 1):
            ws_t.set_column(c, c, 14, pct)
        n = len(trend)
        chart = wb.add_chart({"type": "line"})
        for c, lender in enumerate(trend.columns, start=1):
            chart.add_series({
                "name": ["Trend", 1, c],
                "categories": ["Trend", 2, 0, 1 + n, 0],
                "values": ["Trend", 2, c, 1 + n, c],
                "line": {"color": colors.get(lender, TEXT_SECONDARY), "width": 2},
            })
        chart.set_title({"name": "VS4 share of UPB"})
        chart.set_y_axis({"num_format": "0%", "major_gridlines": {"visible": True,
                          "line": {"color": "#e6e5e0"}}})
        chart.set_x_axis({"name": "Origination month"})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 760, "height": 420})
        ws_t.insert_chart(3, len(trend.columns) + 2, chart)

        # Full history log
        log.to_excel(writer, sheet_name="Log", index=False)
        writer.sheets["Log"].set_column(0, len(log.columns) - 1, 16)

    print(f"wrote {out_path}")


# ----------------------------------------------------------------- PNGs ----

def _style_axes(ax) -> None:
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(TEXT_SECONDARY)
    ax.grid(axis="y", color="#e6e5e0", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1))
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)


def plot_aggregate(agg: pd.DataFrame, settings: dict) -> None:
    exhibits_dir = resolve_path(settings, "exhibits_dir")
    exhibits_dir.mkdir(parents=True, exist_ok=True)
    agg = agg.sort_values("origination_month")
    palette = settings["palette"]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    ax.plot(agg["origination_month"], agg["vs4_share_upb"], color=palette[0],
            linewidth=2, marker="o", markersize=4, label="UPB-weighted")
    ax.plot(agg["origination_month"], agg["vs4_share_loans"], color=palette[1],
            linewidth=2, marker="o", markersize=4, label="Loan-count-weighted")
    _style_axes(ax)
    ax.set_title("VS4 share of GSE deliveries — aggregate", loc="left",
                 fontsize=12, fontweight="bold", color="#0b0b0b")
    ax.legend(frameon=False, fontsize=9, loc="best")
    fig.tight_layout()
    out = exhibits_dir / "vs4_share_aggregate.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def plot_by_lender(folded: pd.DataFrame, settings: dict) -> None:
    exhibits_dir = resolve_path(settings, "exhibits_dir")
    exhibits_dir.mkdir(parents=True, exist_ok=True)
    colors = lender_colors(settings)

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    for lender in settings["focus_lenders"] + [OTHER_LENDER]:
        sub = folded[folded["lender"] == lender].sort_values("origination_month")
        if sub.empty:
            continue
        ax.plot(sub["origination_month"], sub["vs4_share_upb"],
                color=colors[lender], linewidth=2, marker="o", markersize=4, label=lender)
    _style_axes(ax)
    ax.set_title("VS4 share of UPB by lender", loc="left",
                 fontsize=12, fontweight="bold", color="#0b0b0b")
    ax.legend(frameon=False, fontsize=9, loc="best", ncols=2)
    fig.tight_layout()
    out = exhibits_dir / "vs4_share_by_lender.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def main() -> None:
    settings = load_settings()
    snap, log = load_latest_snapshot(settings)
    write_workbook(snap, log, settings)
    plot_aggregate(snap[snap["lender"] == AGGREGATE_LENDER], settings)
    plot_by_lender(fold_to_focus(snap, settings["focus_lenders"]), settings)


if __name__ == "__main__":
    main()
