"""Shared config loading, the common parquet schema, and lender normalization.

Every module in the pipeline goes through here so the schema and the
normalization rules exist in exactly one place.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
LENDERS_PATH = REPO_ROOT / "config" / "lenders.yaml"

# Common parquet schema — both parsers must emit exactly these columns.
# score_model canonical values: "VS4", "FICO", "OTHER", "UNKNOWN".
SCHEMA_COLUMNS = [
    "loan_id",            # str: GSE loan identifier
    "seller_name",        # str: seller name exactly as disclosed
    "lender",             # str: normalized via config/lenders.yaml
    "score_model",        # str: VS4 | FICO | OTHER | UNKNOWN
    "origination_month",  # str: YYYY-MM
    "upb",                # float: original UPB, dollars
    "gse",                # str: freddie | fannie
    "source_file",        # str: raw filename the row came from
]

AGGREGATE_LENDER = "ALL LENDERS"
OTHER_LENDER = "Other"


def load_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def load_lender_map() -> list[tuple[str, list[str]]]:
    """Ordered (normalized_name, [uppercase substring patterns]) pairs."""
    with open(LENDERS_PATH) as f:
        raw = yaml.safe_load(f)
    return [(name, [p.upper() for p in patterns]) for name, patterns in raw.items()]


def normalize_lender(seller_name: str, lender_map: list[tuple[str, list[str]]]) -> str:
    """Map a raw disclosed seller name to a normalized lender name.

    Case-insensitive substring match, first match in config/lenders.yaml order
    wins. Unmatched names pass through title-cased so new sellers are visible
    in the data rather than silently bucketed.
    """
    if not seller_name or not seller_name.strip():
        return "Unknown Seller"
    upper = seller_name.upper()
    for name, patterns in lender_map:
        if any(p in upper for p in patterns):
            return name
    return seller_name.strip().title()


def resolve_path(settings: dict, key: str) -> Path:
    """Resolve a paths: entry from settings.yaml relative to the repo root."""
    return REPO_ROOT / settings["paths"][key]


def month_range(start: str, end: str | None = None) -> list[str]:
    """Inclusive list of YYYY-MM strings from start through end.

    end defaults to the most recent *complete* month (disclosure files for the
    current month are not final).
    """
    if end is None:
        today = dt.date.today()
        last_complete = (today.replace(day=1) - dt.timedelta(days=1))
        end = last_complete.strftime("%Y-%m")
    months = pd.period_range(start=start, end=end, freq="M")
    return [str(m) for m in months]


def validate_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Assert a parser output matches the common schema; reorder columns."""
    missing = set(SCHEMA_COLUMNS) - set(df.columns)
    extra = set(df.columns) - set(SCHEMA_COLUMNS)
    if missing or extra:
        raise ValueError(
            f"Parser output does not match common schema. "
            f"Missing: {sorted(missing)}; unexpected: {sorted(extra)}"
        )
    bad_models = set(df["score_model"].unique()) - {"VS4", "FICO", "OTHER", "UNKNOWN"}
    if bad_models:
        raise ValueError(f"Non-canonical score_model values: {sorted(bad_models)}")
    return df[SCHEMA_COLUMNS]
