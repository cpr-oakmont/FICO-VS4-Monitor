"""One-off probe: how does ramp.com/data/ai-index load its data?"""
import json, pathlib, sys
from playwright.sync_api import sync_playwright

OUT = pathlib.Path("probe/out"); OUT.mkdir(parents=True, exist_ok=True)
URL = "https://ramp.com/data/ai-index"
caught = []

def on_response(resp):
    rt = resp.request.resource_type
    ct = resp.headers.get("content-type", "")
    entry = {"url": resp.url, "status": resp.status, "type": rt, "ctype": ct}
    if rt in ("fetch", "xhr") or "json" in ct or "csv" in ct:
        try:
            body = resp.text()
            entry["size"] = len(body)
            if ("json" in ct or "csv" in ct) and len(body) < 3_000_000:
                name = f"resp_{len(caught):02d}.{'csv' if 'csv' in ct else 'json'}"
                (OUT / name).write_text(body)
                entry["saved"] = name
        except Exception as e:
            entry["err"] = str(e)
    caught.append(entry)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36")
    pg.on("response", on_response)
    try:
        pg.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print("goto:", e, file=sys.stderr)
    pg.wait_for_timeout(15_000)
    pg.mouse.wheel(0, 20000); pg.wait_for_timeout(5_000)
    pg.screenshot(path=str(OUT / "page.png"), full_page=True)
    (OUT / "page.html").write_text(pg.content())
    (OUT / "page.txt").write_text(pg.inner_text("body"))
    links = pg.eval_on_selector_all("a[href]", "els => els.map(e => [e.innerText.trim().slice(0,80), e.href])")
    (OUT / "links.json").write_text(json.dumps(links, indent=1))
    for path in ("/robots.txt", "/legal/terms"):
        try:
            r = pg.request.get("https://ramp.com" + path, timeout=30_000)
            (OUT / ("robots.txt" if "robots" in path else "terms.txt")).write_text(f"{r.status}\n" + r.text()[:200_000])
        except Exception as e:
            print(path, e, file=sys.stderr)
    b.close()

(OUT / "responses.json").write_text(json.dumps(caught, indent=1))
print(f"{len(caught)} responses; saved {sum('saved' in c for c in caught)} data bodies")
for c in caught:
    if c["type"] in ("fetch", "xhr") or "saved" in c:
        print(c["status"], c["type"], c.get("size"), c["url"][:180])
