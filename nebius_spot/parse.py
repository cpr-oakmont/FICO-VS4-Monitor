"""Turn raw daily captures into data/nebius_spot/spot_prices.csv.

Re-runnable over all of raw/ at any time: rows are de-duplicated on
(gpu, region, price_ts, metric), so overlapping 7-day windows from successive
days collapse into one continuous history. If Nebius changes field names,
adjust the KEY_* patterns and re-run; no history is lost.
"""

import csv
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "nebius_spot"
RAW_DIR = DATA / "raw"
OUT = DATA / "spot_prices.csv"

KEY_PRICE = re.compile(r"price|rate|cost|usd|amount|value", re.I)
KEY_GPU = re.compile(r"gpu|platform|preset|model|accelerator|sku", re.I)
KEY_REGION = re.compile(r"region|geo|location|zone|datacenter|dc\b|country", re.I)
KEY_TS = re.compile(r"time|date|ts\b|timestamp|at$|period|day|hour", re.I)

FIELDS = ["capture_date", "gpu", "region", "price_ts", "metric", "price", "source", "extra"]


def scalar(v):
    return isinstance(v, (str, int, float, bool)) or v is None


def to_num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        m = re.fullmatch(r"\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/\s*h(?:ou)?r)?\s*", v)
        if m:
            return float(m.group(1))
    return None


def pick(d, pat):
    for k, v in d.items():
        if pat.search(k) and scalar(v) and v not in (None, ""):
            return k, v
    return None, None


def walk(node, ctx, out, source):
    """Recursively find dicts holding prices; inherit gpu/region from parents."""
    if isinstance(node, list):
        for item in node:
            walk(item, ctx, out, source)
        return
    if not isinstance(node, dict):
        return

    ctx = dict(ctx)
    _, gpu = pick(node, KEY_GPU)
    _, region = pick(node, KEY_REGION)
    if gpu is not None:
        ctx["gpu"] = gpu
    if region is not None:
        ctx["region"] = region

    _, ts = pick(node, KEY_TS)
    for k, v in node.items():
        if KEY_PRICE.search(k) and not KEY_TS.search(k):
            num = to_num(v)
            if num is not None and 0 < num < 1000:
                extra = {kk: vv for kk, vv in node.items() if scalar(vv) and kk != k}
                out.append({
                    "gpu": ctx.get("gpu", ""),
                    "region": ctx.get("region", ""),
                    "price_ts": ts if ts is not None else "",
                    "metric": k,
                    "price": num,
                    "source": source,
                    "extra": json.dumps(extra, default=str)[:500],
                })
    for k, v in node.items():
        if not scalar(v):
            walk(v, ctx, out, source)


def rows_from_tables(tables, source):
    """Fallback: rendered HTML tables -> rows keyed by header."""
    rows = []
    for t in tables:
        if len(t) < 2:
            continue
        header = t[0]
        for r in t[1:]:
            rec = dict(zip(header, r))
            walk(rec, {}, rows, f"{source}#table")
    return rows


def main():
    existing = {}
    if OUT.exists():
        with OUT.open() as f:
            for r in csv.DictReader(f):
                existing[(r["gpu"], r["region"], r["price_ts"], r["metric"], r["capture_date"] if not r["price_ts"] else "")] = r

    for day_dir in sorted(p for p in RAW_DIR.glob("*") if p.is_dir()):
        date = day_dir.name
        for fp in sorted(day_dir.glob("*.json")):
            if fp.name.startswith("_"):
                continue
            blob = json.loads(fp.read_text())
            found = []
            for resp in blob.get("responses", []):
                walk(resp["body"], {}, found, f"{fp.stem}|{resp['url'][:120]}")
            found += rows_from_tables(blob.get("tables", []), fp.stem)
            for r in found:
                r["capture_date"] = date
                # Point-in-time rows (no timestamp) are keyed by capture date;
                # history rows by their own timestamp so overlaps de-dupe.
                key = (str(r["gpu"]), str(r["region"]), str(r["price_ts"]), r["metric"],
                       date if r["price_ts"] == "" else "")
                existing.setdefault(key, r)

    rows = sorted(existing.values(), key=lambda r: (str(r["gpu"]), str(r["region"]), str(r["price_ts"]), r["capture_date"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"{len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
