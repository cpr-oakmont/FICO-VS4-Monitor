# FICO-VS4-Monitor
## Nebius GPU spot price tracker

Nebius's [spot price page](https://nebius.com/spot-prices) only shows 7 days of history. A GitHub Action (`.github/workflows/nebius-spot.yml`) runs twice a day, at 13:17 and 01:17 UTC, and can also be started from the Actions tab. It builds a longer record.

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

## Ramp AI Index tracker

The [Ramp AI Index](https://ramp.com/data/ai-index) measures AI adoption and spend across 85,000+ US businesses from their Ramp card and bill-pay transactions. `.github/workflows/ramp-ai.yml` runs `ramp_ai/collect.py` twice a week (Tue and Sat, 14:23 UTC), and it can also be started from the Actions tab.

The page is server-rendered with its full monthly history embedded, so each run rewrites these files in `data/ramp_ai/` from the latest page:

| File | Grain | What it is |
|---|---|---|
| `adoption_overall.csv` | month | Share of businesses paying for any AI, since Jul 2023 |
| `adoption_vendor.csv` | month × vendor | Share of businesses paying Anthropic, OpenAI, xAI, DeepSeek, Mistral |
| `adoption_census.csv` | month | Census Business Trends and Outlook Survey estimate, for comparison |
| `adoption_sector.csv`, `adoption_size.csv` | month × group | Adoption by NAICS sector and by business size |
| `spend_per_employee.csv` | month | AI spend per employee per month among firms paying for AI (median, top 10%, top 1%) |
| `ai_share_of_spend.csv` | month | AI's share of non-payroll business spend |

Ramp only shows the latest week of tokenomics, compared with the same week a month earlier, so these files are upserted and build history from our own captures:

| File | Grain | What it is |
|---|---|---|
| `token_share.csv` | week × metric × vendor | Share of token spend and token volume by vendor |
| `token_growth.csv` | week | Change in total token spend and volume against the same week a month earlier |

`raw/<timestamp>.json` keeps each run's extracted series. Ramp can revise history, so these are the earlier vintages.

