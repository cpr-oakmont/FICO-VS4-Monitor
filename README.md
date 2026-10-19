# FICO-VS4-Monitor
## Nebius GPU spot price tracker

Nebius's [spot price page](https://nebius.com/spot-prices) only shows 7 days of history. A daily GitHub Action (`.github/workflows/nebius-spot.yml`, 13:17 UTC, can also be started from the Actions tab) builds a longer record.

The page reads from a public, unauthenticated API, and `nebius_spot/collect.py` calls it directly with no browser:

- `GET api.nebius.cloud/billing/v1alpha1/spot-price` returns the current price for every GPU × region market.
- `GET .../spot-price/list-series` returns hourly price history (last price in each hour) per market.

Output in `data/nebius_spot/`:

| File | Grain | Columns |
|---|---|---|
| `spot_hourly.csv` | one row per market-hour | timestamp, gpu, region, price_usd, platform, sku_id, captured_at |
| `spot_snapshots.csv` | one row per market per run | captured_at, gpu, region, price_usd, effective_time, platform, sku_id |
| `raw/<timestamp>.json` | one file per run | raw API responses, the source of record |

Each run requests the last 7 days of history, the most the page shows (a 90-day request was rejected with HTTP 400). Daily runs therefore overlap by about 6 days and the hourly series stays continuous; if a run fails, there is about a week to catch up before a gap opens. Rows are de-duplicated on (sku_id, timestamp). `python nebius_spot/collect.py --rebuild` regenerates both CSVs from `raw/`.

Run locally (standard library only): `python nebius_spot/collect.py`
