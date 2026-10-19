"""Daily collector for Nebius GPU spot (preemptible VM) prices.

nebius.com/spot-prices is backed by a public, unauthenticated JSON API:
  GET /billing/v1alpha1/spot-price               current price per SKU (GPU x region)
  GET /billing/v1alpha1/spot-price/list-series   hourly price history per SKU
The page only shows 7 days of history, so each run saves the raw responses
and merges them into two long-format CSVs that keep growing:

  data/nebius_spot/spot_hourly.csv     one row per (SKU, hour): the history series
  data/nebius_spot/spot_snapshots.csv  one row per (SKU, run): the current quote

Successive daily 7-day windows overlap, so the hourly series is continuous
and de-duplicated on (sku_id, timestamp); a later capture overwrites an
earlier one for the same hour.
"""

import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.nebius.cloud/billing/v1alpha1/spot-price"
HEADERS = {
    "Accept": "application/json",
    "Origin": "https://nebius.com",
    "Referer": "https://nebius.com/spot-prices",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
}
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "nebius_spot"
RAW_DIR = DATA / "raw"
HOURLY = DATA / "spot_hourly.csv"
SNAPSHOTS = DATA / "spot_snapshots.csv"

HOURLY_FIELDS = ["timestamp", "gpu", "region", "price_usd", "platform", "sku_id", "captured_at"]
SNAPSHOT_FIELDS = ["captured_at", "gpu", "region", "price_usd", "effective_time", "platform", "sku_id"]


def get(url: str, params=None, retries: int = 4) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries - 1:
                raise
            print(f"WARN {e}; retrying", file=sys.stderr)
            time.sleep(2 ** (attempt + 1))
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"WARN {e}; retrying", file=sys.stderr)
            time.sleep(2 ** (attempt + 1))


def gpu_name(product_title: str) -> str:
    # "Preemptible NVIDIA® B300 NVLink" -> "B300"
    s = re.sub(r"^Preemptible\s+", "", product_title)
    s = re.sub(r"NVIDIA\S*\s+", "", s)
    return re.sub(r"\s+NVLink$", "", s).strip()


def fetch(lookback_days: int) -> dict:
    current, token = [], None
    while True:
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        page = get(API, params)
        current += page.get("items", [])
        token = page.get("nextPageToken")
        if not token:
            break
    snapshot_time = page.get("snapshotTime")

    sku_ids = sorted({i["skuId"] for i in current})
    # The API rejects windows longer than it retains (90 days -> HTTP 400), so
    # step down to the page's own 7-day window, then shorter ones, as needed.
    for days in sorted({lookback_days, 7, 3, 1}, reverse=True):
        if days > lookback_days:
            continue
        try:
            return {"snapshotTime": snapshot_time, "current": current,
                    "series": fetch_series(sku_ids, days), "_lookback_days": days}
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            print(f"WARN {days}-day history rejected ({e}); trying shorter", file=sys.stderr)
    raise RuntimeError("API rejected every history window")


def fetch_series(sku_ids, days):
    # Round up to the next hour so a window of exactly N days never exceeds N.
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    start = start.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    series = []
    for k in range(0, len(sku_ids), 10):
        resp = get(f"{API}/list-series", {
            "filters.startTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "aggregation.granularity": "GRANULARITY_HOUR_1",
            "aggregation.method": "METHOD_LAST_PRICE",
            "filters.skuIds": sku_ids[k:k + 10],
        })
        series += resp.get("items", [])
    return series


def read_csv(path, key):
    if not path.exists():
        return {}
    with path.open() as f:
        return {key(r): r for r in csv.DictReader(f)}


def write_csv(path, fields, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def merge(captures):
    """Fold raw captures (oldest first) into the two CSVs."""
    hourly = read_csv(HOURLY, lambda r: (r["sku_id"], r["timestamp"]))
    snaps = read_csv(SNAPSHOTS, lambda r: (r["sku_id"], r["captured_at"]))
    for cap in captures:
        captured_at = cap["snapshotTime"] or cap["_captured_at"]
        for i in cap["current"]:
            snaps[(i["skuId"], captured_at)] = {
                "captured_at": captured_at,
                "gpu": gpu_name(i["productTitle"]),
                "region": i["region"],
                "price_usd": i["price"]["value"],
                "effective_time": i.get("effectiveTime", ""),
                "platform": i.get("computeInstance", {}).get("platform", ""),
                "sku_id": i["skuId"],
            }
        for s in cap["series"]:
            for p in s.get("points", []):
                hourly[(s["skuId"], p["timestamp"])] = {
                    "timestamp": p["timestamp"],
                    "gpu": gpu_name(s["productTitle"]),
                    "region": s["region"],
                    "price_usd": p["value"],
                    "platform": s.get("computeInstance", {}).get("platform", ""),
                    "sku_id": s["skuId"],
                    "captured_at": captured_at,
                }
    write_csv(HOURLY, HOURLY_FIELDS, sorted(hourly.values(), key=lambda r: (r["gpu"], r["region"], r["timestamp"])))
    write_csv(SNAPSHOTS, SNAPSHOT_FIELDS, sorted(snaps.values(), key=lambda r: (r["captured_at"], r["gpu"], r["region"])))
    return len(hourly), len(snaps)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=7,
                    help="history to request each run; falls back to shorter windows if rejected")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the CSVs from every raw capture, no fetch")
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if args.rebuild:
        for p in (HOURLY, SNAPSHOTS):
            p.unlink(missing_ok=True)
        caps = []
        for f in sorted(RAW_DIR.glob("*.json")):
            c = json.loads(f.read_text())
            c.setdefault("_captured_at", f.stem)
            caps.append(c)
        print("rebuilt: %d hourly rows, %d snapshot rows" % merge(caps))
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    cap = fetch(args.lookback_days)
    cap["_captured_at"] = now.isoformat(timespec="seconds")
    (RAW_DIR / f"{now:%Y-%m-%dT%H%M}Z.json").write_text(json.dumps(cap, indent=1))

    points = sum(len(s.get("points", [])) for s in cap["series"])
    first = min((p["timestamp"] for s in cap["series"] for p in s.get("points", [])), default="n/a")
    print(f"history window: {cap['_lookback_days']} days")
    print(f"{len(cap['current'])} markets, {len(cap['series'])} series, {points} hourly points (earliest {first})")
    if not cap["current"]:
        print("ERROR: API returned no markets", file=sys.stderr)
        return 1
    print("csv: %d hourly rows, %d snapshot rows" % merge([cap]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
