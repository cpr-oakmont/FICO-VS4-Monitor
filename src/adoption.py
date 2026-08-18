"""Compute VS4 share by lender/month and append a snapshot to the history log.

Usage (from repo root):
    uv run python -m src.adoption

Reads every parquet file in data/processed/, computes per-(lender, month) and
aggregate VS4 shares by loan count and by UPB, and appends the result to
history/adoption_log.csv stamped with today's snapshot_date.

The log is append-only across snapshot dates — each monthly run adds a new
snapshot rather than rewriting history, so restatements in GSE disclosure
files remain visible over time. Re-running on the same day replaces that
day's rows (idempotent), never earlier ones.
"""

from __future__ import annotations

import datetime as dt
import sys

import pandas as pd

from src.common import AGGREGATE_LENDER, load_settings, resolve_path

LOG_COLUMNS = [
    "snapshot_date",     # YYYY-MM-DD the pipeline ran
    "origination_month", # YYYY-MM
    "lender",            # normalized lender, or ALL LENDERS aggregate
    "loans",             # total loans disclosed
    "loans_vs4",         # loans scored with VantageScore 4.0
    "upb",               # total original UPB, $
    "upb_vs4",           # VS4 original UPB, $
    "vs4_share_loans",   # loans_vs4 / loans
    "vs4_share_upb",     # upb_vs4 / upb
]


def load_processed(settings: dict) -> pd.DataFrame:
    processed_dir = resolve_path(settings, "processed_dir")
    files = sorted(processed_dir.glob("*.parquet"))
    if not files:
        sys.exit(f"No parquet files in {processed_dir}. Run the parsers first.")
    df = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    # The same loan can appear in multiple disclosure files (daily then
    # monthly); keep one row per GSE loan ID, preferring the newest file.
    df = df.sort_values("source_file").drop_duplicates(subset=["gse", "loan_id"], keep="last")
    return df


def compute_adoption(loans: pd.DataFrame) -> pd.DataFrame:
    """Per-(lender, origination_month) VS4 share, plus an ALL LENDERS row set."""
    is_vs4 = loans["score_model"] == "VS4"
    upb = loans["upb"].fillna(0.0)
    loans = loans.assign(is_vs4=is_vs4, upb=upb, upb_if_vs4=upb.where(is_vs4, 0.0))

    def agg(group_cols: list[str]) -> pd.DataFrame:
        return loans.groupby(group_cols, as_index=False).agg(
            loans=("loan_id", "size"),
            loans_vs4=("is_vs4", "sum"),
            upb=("upb", "sum"),
            upb_vs4=("upb_if_vs4", "sum"),
        )

    by_lender = agg(["origination_month", "lender"])
    overall = agg(["origination_month"]).assign(lender=AGGREGATE_LENDER)

    out = pd.concat([by_lender, overall], ignore_index=True)
    out["vs4_share_loans"] = (out["loans_vs4"] / out["loans"]).round(6)
    out["vs4_share_upb"] = (out["upb_vs4"] / out["upb"].replace(0, pd.NA)).astype(float).round(6)
    return out.sort_values(["origination_month", "lender"]).reset_index(drop=True)


def append_snapshot(adoption: pd.DataFrame, settings: dict,
                    snapshot_date: str | None = None) -> pd.DataFrame:
    history_file = resolve_path(settings, "history_file")
    snapshot_date = snapshot_date or dt.date.today().isoformat()
    snap = adoption.assign(snapshot_date=snapshot_date)[LOG_COLUMNS]

    if history_file.exists() and history_file.stat().st_size > 0:
        log = pd.read_csv(history_file, dtype={"snapshot_date": str, "origination_month": str})
        # Idempotent re-run: replace only today's rows, never earlier snapshots.
        log = log[log["snapshot_date"] != snapshot_date]
    else:
        log = pd.DataFrame(columns=LOG_COLUMNS)

    log = pd.concat([log, snap], ignore_index=True)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    log.to_csv(history_file, index=False)
    return snap


def main() -> None:
    settings = load_settings()
    loans = load_processed(settings)
    adoption = compute_adoption(loans)
    snap = append_snapshot(adoption, settings)

    agg = snap[snap["lender"] == AGGREGATE_LENDER].tail(3)
    print(f"Appended snapshot {snap['snapshot_date'].iloc[0]}: "
          f"{len(snap)} rows, months {snap['origination_month'].min()}..{snap['origination_month'].max()}")
    print("Aggregate VS4 share (last 3 months):")
    for _, r in agg.iterrows():
        print(f"  {r['origination_month']}: {r['vs4_share_loans']:.1%} of loans, "
              f"{r['vs4_share_upb']:.1%} of UPB ({r['loans']:,} loans)")


if __name__ == "__main__":
    main()
