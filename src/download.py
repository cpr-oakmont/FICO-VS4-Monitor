"""Incremental fetch of GSE loan-level disclosure (ILLD) files.

Usage (from repo root):
    uv run python -m src.download --check          # test whether URLs resolve, and whether auth is needed
    uv run python -m src.download                  # fetch all missing months for all sources
    uv run python -m src.download --source freddie --since 2025-09

Behavior:
  - URL templates live in config/settings.yaml (sources.<gse>.urls) with
    {yyyymm}/{yyyy}/{mm} placeholders; one file per month per template.
  - Incremental: a month whose file already exists in data/raw/<gse>/ is
    skipped. Downloads write to a .part file and rename on success, so an
    interrupted run never leaves a truncated file that would be skipped later.
  - Credentials: --check always probes WITHOUT auth first, so we know whether
    a source is public. If settings.yaml sets auth: env for a source, real
    downloads use HTTP basic auth with credentials read from .env (gitignored,
    loaded via python-dotenv). Credentials never live in config or code.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from src.common import REPO_ROOT, load_settings, month_range

TIMEOUT = 60
RETRIES = 3
CHUNK = 1 << 20  # 1 MiB

# Response signatures that mean "you need to log in" even when the server
# says 200 (some portals redirect to an HTML login page instead of 401ing).
LOGIN_HINTS = ("login", "signin", "sign-in", "auth")


def expand_url(template: str, month: str) -> str:
    yyyy, mm = month.split("-")
    return template.format(yyyymm=f"{yyyy}{mm}", yyyy=yyyy, mm=mm)


def filename_for(url: str) -> str:
    name = Path(urlparse(url).path).name
    return name or "download.bin"


def probe(url: str, auth: tuple[str, str] | None = None) -> dict:
    """HEAD the URL (GET fallback); report status and whether auth looks required."""
    sess = requests.Session()
    try:
        resp = sess.head(url, timeout=TIMEOUT, allow_redirects=True, auth=auth)
        if resp.status_code in (405, 501):  # server rejects HEAD, try a 0-byte GET
            resp = sess.get(url, timeout=TIMEOUT, allow_redirects=True, auth=auth, stream=True)
            resp.close()
    except requests.RequestException as exc:
        return {"url": url, "ok": False, "status": None, "auth_required": None,
                "detail": f"request failed: {exc}"}

    final_url = resp.url.lower()
    ctype = resp.headers.get("Content-Type", "")
    auth_required = (
        resp.status_code in (401, 403)
        or (resp.ok and any(h in final_url for h in LOGIN_HINTS))
        or (resp.ok and "text/html" in ctype and url.lower().endswith((".zip", ".gz", ".txt", ".csv")))
    )
    return {
        "url": url,
        "ok": resp.ok and not auth_required,
        "status": resp.status_code,
        "auth_required": auth_required,
        "detail": f"content-type={ctype or '?'} length={resp.headers.get('Content-Length', '?')}"
                  + (f" (redirected to {resp.url})" if resp.url != url else ""),
    }


def get_auth(source_cfg: dict) -> tuple[str, str] | None:
    """Resolve basic-auth credentials from .env for auth: env sources."""
    if source_cfg.get("auth", "none") != "env":
        return None
    load_dotenv(REPO_ROOT / ".env")
    env = source_cfg.get("auth_env", {})
    user = os.environ.get(env.get("username", ""), "")
    pwd = os.environ.get(env.get("password", ""), "")
    if not user or not pwd:
        sys.exit(
            f"Source is configured with auth: env but {env.get('username')}/"
            f"{env.get('password')} are not set. Copy .env.example to .env and fill them in."
        )
    return (user, pwd)


def download_file(url: str, dest: Path, auth: tuple[str, str] | None) -> bool:
    """Fetch one file with retries; atomic .part rename. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, RETRIES + 1):
        try:
            with requests.get(url, timeout=TIMEOUT, stream=True, auth=auth) as resp:
                if resp.status_code == 404:
                    print(f"  not published yet (404): {url}")
                    return False
                resp.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(CHUNK):
                        f.write(chunk)
            part.rename(dest)
            print(f"  downloaded {dest.name} ({dest.stat().st_size:,} bytes)")
            return True
        except requests.RequestException as exc:
            wait = 2 ** attempt
            print(f"  attempt {attempt}/{RETRIES} failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    part.unlink(missing_ok=True)
    print(f"  FAILED after {RETRIES} attempts: {url}")
    return False


def run_check(settings: dict, sources: list[str]) -> int:
    """Probe each configured URL template unauthenticated and report."""
    months = month_range(settings["start_month"])
    latest = months[-1]
    any_fail = False
    for gse in sources:
        cfg = settings["sources"][gse]
        if not cfg.get("urls"):
            print(f"[{gse}] no URLs configured in config/settings.yaml — skipping")
            continue
        print(f"[{gse}] probing without authentication (month {latest}):")
        for template in cfg["urls"]:
            r = probe(expand_url(template, latest))
            if r["ok"]:
                verdict = "OK, public"
            elif r["auth_required"]:
                verdict = "AUTH REQUIRED — set auth: env in settings.yaml and fill .env"
            else:
                verdict = "UNREACHABLE"
                any_fail = True
            print(f"  [{verdict}] status={r['status']} {r['url']}\n    {r['detail']}")
    return 1 if any_fail else 0


def run_fetch(settings: dict, sources: list[str], since: str | None) -> None:
    for gse in sources:
        cfg = settings["sources"][gse]
        if not cfg.get("urls"):
            print(f"[{gse}] no URLs configured — nothing to fetch")
            continue
        raw_dir = REPO_ROOT / cfg["raw_dir"]
        months = month_range(since or settings["start_month"])
        auth = get_auth(cfg)
        print(f"[{gse}] {len(months)} month(s) x {len(cfg['urls'])} template(s) -> {raw_dir}")
        for month in months:
            for template in cfg["urls"]:
                url = expand_url(template, month)
                dest = raw_dir / filename_for(url)
                if dest.exists():
                    print(f"  have {dest.name}, skipping")
                    continue
                download_file(url, dest, auth)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="probe configured URLs without auth and report; no downloads")
    ap.add_argument("--source", choices=["freddie", "fannie"],
                    help="limit to one GSE (default: both)")
    ap.add_argument("--since", metavar="YYYY-MM",
                    help="override start month for this run")
    args = ap.parse_args()

    settings = load_settings()
    sources = [args.source] if args.source else list(settings["sources"])
    if args.check:
        sys.exit(run_check(settings, sources))
    run_fetch(settings, sources, args.since)


if __name__ == "__main__":
    main()
