"""Daily snapshot of Nebius GPU spot prices (https://nebius.com/spot-prices).

The public page only shows 7 days of history, so this script captures it once a
day and appends to a long-format CSV we own.

Strategy (robust to not knowing the page's internals in advance):
  1. Load the page in headless Chromium for each GPU / range combo and record
     every JSON response the page fetches, plus any embedded JSON
     (__NEXT_DATA__, application/json script tags) and the rendered tables.
  2. Save all of it raw to data/nebius_spot/raw/<date>/ so history is never
     lost even if the parser below needs adjusting later (re-run parse.py).
  3. parse.py walks the raw JSON, pulls out price records (gpu, region, price,
     timestamp, ...) and upserts them into data/nebius_spot/spot_prices.csv.
"""

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

BASE_URL = "https://nebius.com/spot-prices"
DEFAULT_GPUS = ["B300", "B200", "H200", "H100", "RTX PRO 6000", "L40S"]
ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "nebius_spot" / "raw"


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:80]


def capture(page, url: str, out_dir: pathlib.Path, tag: str) -> int:
    captured = []

    def on_response(resp):
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype or resp.request.resource_type not in ("fetch", "xhr", "document"):
            return
        try:
            body = resp.json()
        except Exception:
            return
        captured.append({"url": resp.url, "status": resp.status, "body": body})

    page.on("response", on_response)
    page.goto(url, wait_until="networkidle", timeout=90_000)
    page.wait_for_timeout(3_000)
    page.remove_listener("response", on_response)

    # Embedded JSON (Next.js / hydration payloads).
    embedded = page.evaluate(
        """() => [...document.querySelectorAll('script[type="application/json"], script#__NEXT_DATA__')]
                .map(s => ({id: s.id, text: s.textContent}))"""
    )
    for e in embedded:
        try:
            captured.append({"url": f"embedded:{e['id'] or 'script'}", "status": 200, "body": json.loads(e["text"])})
        except Exception:
            pass

    # Rendered tables as a fallback that doesn't depend on any API shape.
    tables = page.evaluate(
        """() => [...document.querySelectorAll('table')].map(t =>
                [...t.querySelectorAll('tr')].map(r =>
                    [...r.querySelectorAll('th,td')].map(c => c.innerText.trim())))"""
    )

    (out_dir / f"{tag}.json").write_text(
        json.dumps({"page_url": url, "responses": captured, "tables": tables}, indent=1, default=str)
    )
    (out_dir / f"{tag}.txt").write_text(page.inner_text("body"))
    return len(captured)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpus", nargs="*", default=DEFAULT_GPUS)
    ap.add_argument("--range", default="7d", help="history range query param")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    out_dir = RAW_DIR / now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ))
        # Default view (current prices across all GPUs/regions).
        total += capture(page, BASE_URL, out_dir, "default")
        # One view per GPU so the 7-day history for each is captured too.
        for gpu in args.gpus:
            url = f"{BASE_URL}?historyGpus={gpu}&historyRange={args.range}&period={args.range}"
            try:
                total += capture(page, url, out_dir, f"gpu_{slug(gpu)}")
            except Exception as e:  # keep going; one bad GPU shouldn't kill the run
                print(f"WARN {gpu}: {e}", file=sys.stderr)
        browser.close()

    (out_dir / "_meta.json").write_text(json.dumps({"captured_at": now.isoformat(), "json_payloads": total}))
    print(f"captured {total} JSON payloads -> {out_dir}")
    if total == 0:
        print("ERROR: no JSON captured; page structure may have changed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
