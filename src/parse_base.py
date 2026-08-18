"""Shared parsing engine for the two GSE parsers.

parse_freddie.py / parse_fannie.py are thin wrappers around this module. All
field positions, delimiters, and score-model code mappings come from the
`layout` block in config/settings.yaml — nothing here guesses a file format.
Until a layout is confirmed and filled in, the parsers exit with instructions
instead of producing wrong data. See CLAUDE.md ("Adding a file layout") for
the layout spec.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.common import (
    REPO_ROOT,
    load_lender_map,
    load_settings,
    normalize_lender,
    resolve_path,
    validate_schema,
)

CHUNKSIZE = 1_000_000

# Schema fields each layout must locate in the raw file.
REQUIRED_FIELDS = ["loan_id", "seller_name", "score_model", "origination_date", "upb"]


def require_layout(settings: dict, gse: str) -> dict:
    layout = settings["sources"][gse].get("layout")
    if not layout:
        sys.exit(
            f"No file layout configured for {gse}.\n"
            f"Fill in sources.{gse}.layout in config/settings.yaml with the "
            f"confirmed ILLD layout (see CLAUDE.md, 'Adding a file layout'). "
            f"This parser will not guess field positions."
        )
    missing = [f for f in REQUIRED_FIELDS if f not in layout.get("fields", {})]
    if missing:
        sys.exit(f"sources.{gse}.layout.fields is missing: {missing}")
    return layout


def parse_file(path: Path, layout: dict, gse: str,
               lender_map: list[tuple[str, list[str]]]) -> pd.DataFrame:
    """Parse one raw disclosure file into the common schema."""
    fields = layout["fields"]
    has_header = bool(layout.get("has_header", False))
    usecols = list(fields.values())

    chunks = []
    reader = pd.read_csv(
        path,
        sep=layout.get("delimiter", "|"),
        header=0 if has_header else None,
        usecols=usecols,
        dtype=str,
        compression="infer",
        chunksize=CHUNKSIZE,
        low_memory=False,
    )
    for chunk in reader:
        chunks.append(_map_chunk(chunk, layout, gse, path.name, lender_map))
    if not chunks:
        return pd.DataFrame()
    return validate_schema(pd.concat(chunks, ignore_index=True))


def _map_chunk(raw: pd.DataFrame, layout: dict, gse: str, source_file: str,
               lender_map: list[tuple[str, list[str]]]) -> pd.DataFrame:
    f = layout["fields"]
    df = pd.DataFrame()
    df["loan_id"] = raw[f["loan_id"]].str.strip()
    df["seller_name"] = raw[f["seller_name"]].fillna("").str.strip()

    lender_cache = {s: normalize_lender(s, lender_map) for s in df["seller_name"].unique()}
    df["lender"] = df["seller_name"].map(lender_cache)

    model_map = {str(k).strip().upper(): v for k, v in layout.get("score_model_map", {}).items()}
    default = layout.get("score_model_default", "UNKNOWN")
    df["score_model"] = (
        raw[f["score_model"]].fillna("").str.strip().str.upper()
        .map(model_map).fillna(default)
    )

    dates = pd.to_datetime(
        raw[f["origination_date"]].str.strip(),
        format=layout.get("origination_date_format", "%Y%m"),
        errors="coerce",
    )
    df["origination_month"] = dates.dt.strftime("%Y-%m")

    df["upb"] = pd.to_numeric(raw[f["upb"]], errors="coerce")
    df["gse"] = gse
    df["source_file"] = source_file
    return df


def run(gse: str) -> None:
    """CLI shared by both parsers: parse raw files -> data/processed parquet."""
    ap = argparse.ArgumentParser(description=f"Parse {gse} ILLD files to the common parquet schema.")
    ap.add_argument("--file", type=Path, help="parse a single raw file instead of the whole raw dir")
    ap.add_argument("--force", action="store_true", help="re-parse files whose parquet already exists")
    args = ap.parse_args()

    settings = load_settings()
    layout = require_layout(settings, gse)
    lender_map = load_lender_map()
    raw_dir = REPO_ROOT / settings["sources"][gse]["raw_dir"]
    out_dir = resolve_path(settings, "processed_dir")
    out_dir.mkdir(parents=True, exist_ok=True)

    files = [args.file] if args.file else sorted(
        p for p in raw_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    if not files:
        sys.exit(f"No raw files in {raw_dir}. Run: uv run python -m src.download")

    for path in files:
        out_path = out_dir / f"{gse}_{path.stem}.parquet"
        if out_path.exists() and not args.force:
            print(f"have {out_path.name}, skipping (use --force to re-parse)")
            continue
        print(f"parsing {path.name} ...")
        df = parse_file(path, layout, gse, lender_map)
        n_unknown = int((df["score_model"] == "UNKNOWN").sum())
        if n_unknown:
            print(f"  WARNING: {n_unknown:,}/{len(df):,} rows have unmapped score_model codes "
                  f"— extend score_model_map in settings.yaml")
        df.to_parquet(out_path, index=False)
        print(f"  wrote {out_path.name}: {len(df):,} loans, "
              f"{df['origination_month'].min()}..{df['origination_month'].max()}")
