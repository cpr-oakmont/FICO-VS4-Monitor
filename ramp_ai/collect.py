"""Collector for the Ramp AI Index (https://ramp.com/data/ai-index).

The page is server-rendered: its full monthly history (adoption overall, by
vendor, sector, size and state, the Census comparison, spend per employee,
AI share of spend) is embedded as JSON in the Next.js payload, and the
tokenomics table (share of token spend and volume by vendor, latest week vs
the same week a month earlier) is in the HTML. Ramp publishes no token
history, so token_share.csv builds up from our own captures.

Each run:
  - saves the extracted series to data/ramp_ai/raw/<timestamp>.json
    (Ramp can revise history; the raw captures keep each vintage),
  - rewrites the monthly CSVs from the latest capture,
  - upserts the tokenomics rows into token_share.csv / token_growth.csv.
"""

import csv
import datetime as dt
import html
import json
import pathlib
import re
import sys
import time
import urllib.request

URL = "https://ramp.com/data/ai-index"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ramp_ai"
RAW_DIR = DATA / "raw"

# Payload key -> (csv name, columns). Columns are kept in this order.
SERIES = {
    "adoptionOverall": ("adoption_overall.csv", ["date_month", "adoption_rate_pct", "mom_change_pp", "yoy_change_pp"]),
    "adoptionVendor": ("adoption_vendor.csv", ["date_month", "vendor", "adoption_rate_pct", "mom_change_pp"]),
    "adoptionUsEstimate": ("adoption_census.csv", ["date_month", "adoption_rate_pct", "mom_change_pp", "question_version"]),
    "adoptionIndustry": ("adoption_sector.csv", ["date_month", "naics_sector", "adoption_rate_pct", "mom_change_pp"]),
    "adoptionSize": ("adoption_size.csv", ["date_month", "business_size", "adoption_rate_pct", "mom_change_pp"]),
    "spendPerEmployee": ("spend_per_employee.csv", ["date_month", "median_pepm", "top_10_percent_median_pepm", "top_1_percent_median_pepm", "business_count"]),
}
REQUIRED = ("adoptionOverall", "adoptionVendor", "adoptionUsEstimate")


def fetch_html() -> str:
    for attempt in range(3):
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            if "adoptionOverall" in text:
                return text
            print("WARN plain fetch lacks data payload; trying a browser", file=sys.stderr)
            break
        except Exception as e:
            print(f"WARN fetch: {e}", file=sys.stderr)
            time.sleep(2 ** (attempt + 1))
    from playwright.sync_api import sync_playwright  # only needed if the plain fetch is blocked
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(10_000)
        text = page.content()
        browser.close()
    return text


def payload(page_html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page_html, re.S)
    return json.loads('"' + "".join(chunks) + '"') if chunks else ""


def extract_series(blob: str) -> dict:
    dec = json.JSONDecoder()
    out = {}
    for key in list(SERIES) + ["spendShareData"]:
        m = re.search(r'"%s":([\[{])' % key, blob)
        if not m:
            continue
        out[key], _ = dec.raw_decode(blob, m.start(1))
    return out


def extract_tokenomics(page_html: str) -> dict:
    text = html.unescape(re.sub(r"<[^>]+>", "|", page_html))
    text = re.sub(r"\s*\|[\s|]*", "|", text)
    weeks = re.search(r"\|Metric\|Week ending ([^|]+)\|Week ending ([^|]+)\|Change\|", text)
    rows = re.findall(r"\|Share of token (spend|volume): ([^|]+)\|([\d.]+)%\|([\d.]*)%?\|", text)
    growth = re.search(r"Total token spending (rose|fell) ([\d.]+)%\. Total token volume (rose|fell) ([\d.]+)%", text)
    if not weeks:
        return {}
    to_iso = lambda s: dt.datetime.strptime(s.strip(), "%b %d, %Y").date().isoformat()
    latest, prior = to_iso(weeks.group(1)), to_iso(weeks.group(2))
    shares = []
    for metric, vendor, now, before in rows:
        shares.append({"week_ending": latest, "metric": metric, "vendor": vendor.strip(), "share_pct": float(now)})
        if before:
            shares.append({"week_ending": prior, "metric": metric, "vendor": vendor.strip(), "share_pct": float(before)})
    out = {"latest_week": latest, "prior_week": prior, "shares": shares}
    if growth:
        sign = lambda w: 1 if w == "rose" else -1
        out["growth"] = {"week_ending": latest, "vs_week_ending": prior,
                         "spend_change_pct": sign(growth.group(1)) * float(growth.group(2)),
                         "volume_change_pct": sign(growth.group(3)) * float(growth.group(4))}
    return out


def write_csv(path, fields, rows):
    # Ramp's payload marks missing values as "$undefined"; write them blank.
    clean = lambda v: "" if v is None or v == "$undefined" else v
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: clean(r.get(k)) for k in fields} for r in rows)


def upsert(path, fields, rows, key):
    existing = {}
    if path.exists():
        with path.open() as f:
            existing = {key(r): r for r in csv.DictReader(f)}
    for r in rows:
        existing[key(r)] = {k: r.get(k, "") for k in fields}
    write_csv(path, fields, sorted(existing.values(), key=lambda r: tuple(str(r[k]) for k in fields)))
    return len(existing)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    page_html = fetch_html()
    series = extract_series(payload(page_html))
    tokens = extract_tokenomics(page_html)

    missing = [k for k in REQUIRED if k not in series]
    if missing:
        print(f"ERROR: page structure changed; missing {missing}", file=sys.stderr)
        return 1

    captured_at = now.isoformat(timespec="seconds")
    (RAW_DIR / f"{now:%Y-%m-%dT%H%M}Z.json").write_text(
        json.dumps({"captured_at": captured_at, "series": series, "tokenomics": tokens}, separators=(",", ":")))

    for key, (name, cols) in SERIES.items():
        if key in series:
            write_csv(DATA / name, cols, series[key])
    share = series.get("spendShareData", {}).get("overall")
    if share:
        write_csv(DATA / "ai_share_of_spend.csv", ["date_month", "ai_share_of_spend_pct"],
                  [{"date_month": r["my_date"], "ai_share_of_spend_pct": round(100 * r["ai_share_of_tpv"], 3)} for r in share])

    if tokens.get("shares"):
        for r in tokens["shares"]:
            r["captured_at"] = captured_at
        n = upsert(DATA / "token_share.csv", ["week_ending", "metric", "vendor", "share_pct", "captured_at"],
                   tokens["shares"], lambda r: (r["week_ending"], r["metric"], r["vendor"]))
        print(f"token_share.csv: {n} rows (latest week {tokens['latest_week']})")
    else:
        print("WARN: tokenomics table not found", file=sys.stderr)
    if tokens.get("growth"):
        upsert(DATA / "token_growth.csv", ["week_ending", "vs_week_ending", "spend_change_pct", "volume_change_pct"],
               [tokens["growth"]], lambda r: (r["week_ending"],))

    last = series["adoptionOverall"][-1]
    print(f"adoption through {last['date_month']}: {last['adoption_rate_pct']}%; "
          + ", ".join(f"{k}={len(v) if isinstance(v, list) else 'obj'}" for k, v in series.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
