# FICO-VS4-Monitor
## Nebius GPU spot price tracker

Nebius's [spot price page](https://nebius.com/spot-prices) only shows 7 days of history. A daily GitHub Action (`.github/workflows/nebius-spot.yml`, 13:17 UTC, can also be started from the Actions tab) captures it and builds a longer history.

- `nebius_spot/scrape.py` loads the page in headless Chromium, the default view plus one view per GPU, and saves every JSON payload, embedded hydration data and rendered table to `data/nebius_spot/raw/<date>/`.
- `nebius_spot/parse.py` pulls price records (GPU, region, timestamp, metric, price) out of the raw files and writes them to `data/nebius_spot/spot_prices.csv`. History points are de-duplicated on their own timestamp, so overlapping 7-day windows merge into one series. Point-in-time quotes are kept once per capture date.

The raw captures are the source of record. If Nebius renames fields, change the `KEY_*` patterns in `parse.py` and re-run it to rebuild the CSV from all past captures.

Run locally: `pip install -r nebius_spot/requirements.txt && python -m playwright install chromium && python nebius_spot/scrape.py && python nebius_spot/parse.py`
