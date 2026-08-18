# fico-vs4-monitor

Tracks VantageScore 4.0 (VS4) adoption in GSE loan-level disclosure data, as
an input to an equity research thesis on FICO. Since FHFA approved VS4 for
GSE delivery, some large sellers (UWM, Rocket, AmeriSave, PennyMac) run a
portion of their volume through VS4 instead of Classic FICO. This repo
measures VS4 share by lender and in aggregate, monthly, and produces an
Excel adoption monitor (`output/adoption_monitor.xlsx`) for the PM.

## Data sources

- **Freddie Mac** loan-level disclosure files (daily and monthly ILLD),
  which include the credit score model used per loan.
- **Fannie Mae** loan-level disclosure files (same concept, different layout).

Exact file URLs and layouts are configured in `config/settings.yaml`
(`sources.<gse>.urls` and `sources.<gse>.layout`). **Both are currently
unset** — they must come from the analyst / the GSEs' published file specs.
The parsers deliberately refuse to run on a null layout: never guess field
positions, delimiters, or score-model codes. Raw files are governed by GSE
terms of use — they stay in gitignored `data/raw/` and are never committed.

## Pipeline (run in this order)

```bash
uv sync                                   # once, installs deps into .venv
uv run python -m src.download --check     # probe URLs WITHOUT auth first
uv run python -m src.download             # incremental fetch of missing months
uv run python -m src.parse_freddie        # raw -> data/processed/*.parquet
uv run python -m src.parse_fannie
uv run python -m src.adoption             # compute shares, append history log
uv run python -m src.exhibits             # regenerate xlsx + PNG exhibits
```

Each stage is incremental and idempotent: download skips files already on
disk, parsers skip raw files whose parquet exists (`--force` to redo),
adoption replaces same-day snapshot rows only, exhibits are fully
regenerated from the history log every run.

## Repository layout

```
config/
  settings.yaml        file URLs, layouts, paths, focus lenders, chart palette
  lenders.yaml         raw seller-name -> normalized lender mapping
data/
  raw/freddie/         downloaded ILLD files   (gitignored)
  raw/fannie/
  processed/           common-schema parquet   (gitignored)
src/
  common.py            settings/lender-map loading, schema, month helpers
  download.py          incremental fetch; --check probes URLs unauthenticated
  parse_base.py        shared layout-driven parsing engine
  parse_freddie.py     Freddie wrapper (GSE-specific quirks live here)
  parse_fannie.py      Fannie wrapper
  adoption.py          VS4 share by lender/month -> history/adoption_log.csv
  exhibits.py          adoption_monitor.xlsx + output/exhibits/*.png
history/
  adoption_log.csv     append-only monthly snapshots (COMMITTED — the record)
output/                generated deliverables (gitignored, regenerable)
notebooks/             ad-hoc analysis
```

## Common parquet schema

Both parsers must emit exactly these columns (`src/common.py:SCHEMA_COLUMNS`,
enforced by `validate_schema`):

| column | type | notes |
|---|---|---|
| `loan_id` | str | GSE loan identifier |
| `seller_name` | str | exactly as disclosed |
| `lender` | str | normalized via `config/lenders.yaml` |
| `score_model` | str | canonical: `VS4`, `FICO`, `OTHER`, `UNKNOWN` |
| `origination_month` | str | `YYYY-MM` |
| `upb` | float | original UPB, dollars |
| `gse` | str | `freddie` \| `fannie` |
| `source_file` | str | raw filename (provenance / dedup tiebreak) |

The same loan can appear in a daily file and later in the monthly ILLD;
`adoption.py` dedupes on (`gse`, `loan_id`), keeping the newest source file.

## Adding a file layout

When the exact ILLD spec is confirmed, fill `sources.<gse>.layout` in
`config/settings.yaml`:

```yaml
layout:
  delimiter: "|"
  has_header: false            # if true, field values below are column NAMES
  fields:                      # 0-based positions (or names) in the raw file
    loan_id: 0
    seller_name: 12
    score_model: 33
    origination_date: 3
    upb: 10
  origination_date_format: "%Y%m"
  score_model_map:             # raw code -> canonical value (UPPERCASED keys)
    "V4": VS4
    "F": FICO
  score_model_default: UNKNOWN # unmapped codes; parser warns when these appear
```

Verify against a real file before trusting output: parse one month, check
row counts against the GSE's published summary, spot-check seller names, and
confirm no material `UNKNOWN` score-model share (the parser prints a warning
with the unmapped count).

## Conventions

- **Credentials**: only in `.env` (gitignored; template in `.env.example`),
  loaded by `src/download.py` via python-dotenv. Never in `config/*.yaml`,
  code, or anything committed. `--check` always probes without auth first so
  we know whether a source is actually public.
- **`history/adoption_log.csv` is append-only** across snapshot dates. Each
  monthly run appends rows stamped `snapshot_date`; earlier snapshots are
  never rewritten, so GSE restatements stay visible. Re-running the same day
  replaces only that day's rows.
- **Lender normalization**: case-insensitive substring match against
  `config/lenders.yaml`, first match wins. Unmatched sellers pass through
  title-cased (visible, not silently bucketed) and fold into "Other" in
  exhibits. Verify seed patterns against real seller names in month one.
- **VS4 share is reported both loan-count- and UPB-weighted** — quote the
  UPB-weighted number to the PM unless stated otherwise.
- **Exhibits read only the history log**, never raw loan data, so the
  deliverable is always reproducible from what's committed.
- **Chart colors**: validated colorblind-safe palette in `settings.yaml`;
  focus lenders own fixed color slots (`focus_lenders` order) so a lender
  never changes color between exhibits or months. One y-axis per chart.
- **Dependencies**: managed with `uv` (`pyproject.toml` + `uv.lock`);
  run everything through `uv run`.
- Months are `YYYY-MM` strings everywhere; the current (incomplete) month is
  never fetched or reported.

## Open TODOs

1. Fill in `sources.*.urls` with the exact ILLD URLs (analyst to provide),
   then run `uv run python -m src.download --check` — if a source needs
   login, set `auth: env` for it and add credentials to `.env`.
2. Fill in `sources.*.layout` from the confirmed Freddie/Fannie file specs
   (analyst to provide layouts — do not guess).
3. Verify `config/lenders.yaml` patterns and `start_month` against the first
   real parsed month.
