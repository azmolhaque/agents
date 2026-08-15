#!/usr/bin/env python3
"""Gather the Phase 1 HTML fixtures.

Run this on the Pi (or any machine with open outbound HTTPS). The build container's
network policy only permits a small allowlist, so the fixtures cannot be collected
there -- see docs/BENCHMARKS.md.

    python scripts/fetch_fixtures.py            # fetch anything missing
    python scripts/fetch_fixtures.py --force    # re-fetch everything

This is a scaled-down rehearsal of the Phase 2 Harvester and obeys the same rules,
because a fixture-gathering script that ignores robots.txt would be the first
passive-only violation in the repo:

  * robots.txt parsed and obeyed, per URL
  * a real User-Agent naming Cindrasec with a contact URL
  * >= FETCH_MIN_INTERVAL_SECONDS between requests to the same host
  * public pages only -- no logins, no paywalls, no query-string probing

URLs live in ``tests/fixtures/urls.txt`` so the set is reviewable in a diff. Each page
is written with its sha256 recorded in ``manifest.json``, which is what lets a later
run prove the corpus has not silently drifted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.robotparser
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "html"
URL_LIST = REPO_ROOT / "tests" / "fixtures" / "urls.txt"
MANIFEST = REPO_ROOT / "tests" / "fixtures" / "manifest.json"

USER_AGENT = "CindrasecLeadsBot/0.1 (+https://cindrasec.com/bot; contact@cindrasec.com)"
MIN_INTERVAL_SECONDS = 3.0
MAX_BYTES = 900_000
TIMEOUT = 30.0


def load_urls() -> list[tuple[str, str]]:
    """Read ``slug<TAB>url`` lines, ignoring blanks and comments."""
    if not URL_LIST.is_file():
        sys.exit(f"missing {URL_LIST}; add one 'slug<TAB>url' per line")
    entries: list[tuple[str, str]] = []
    for raw in URL_LIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            print(f"  skip malformed line: {line!r}")
            continue
        entries.append((parts[0], parts[1]))
    return entries


def robots_allows(client: httpx.Client, url: str, cache: dict[str, object]) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in cache:
        parser = urllib.robotparser.RobotFileParser()
        try:
            response = client.get(f"{origin}/robots.txt", timeout=10.0)
            parser.parse(response.text.splitlines() if response.status_code == 200 else [])
        except httpx.HTTPError:
            # No robots.txt reachable: default to allowing, which is what the standard
            # says, but log it so the decision is visible.
            print(f"  ! robots.txt unreachable for {origin}, proceeding")
            parser.parse([])
        cache[origin] = parser
    parser_obj = cache[origin]
    assert isinstance(parser_obj, urllib.robotparser.RobotFileParser)
    return parser_obj.can_fetch(USER_AGENT, url)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-fetch pages already present")
    args = ap.parse_args()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    entries = load_urls()
    manifest: dict[str, dict[str, object]] = {}
    if MANIFEST.is_file():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    robots_cache: dict[str, object] = {}
    last_hit: dict[str, float] = defaultdict(float)
    fetched = skipped = failed = 0

    with httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=TIMEOUT,
    ) as client:
        for slug, url in entries:
            target = FIXTURE_DIR / f"{slug}.html"
            if target.exists() and not args.force:
                skipped += 1
                continue

            host = urlparse(url).netloc
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last_hit[host])
            if wait > 0:
                time.sleep(wait)

            if not robots_allows(client, url, robots_cache):
                print(f"  ROBOTS-DENIED  {slug:<24} {url}")
                failed += 1
                continue

            try:
                response = client.get(url)
                last_hit[host] = time.monotonic()
                response.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"  FAIL           {slug:<24} {type(exc).__name__}: {exc}")
                failed += 1
                continue

            body = response.text[:MAX_BYTES]
            if len(body) < 3000:
                print(f"  TOO-SMALL      {slug:<24} {len(body)} bytes (JS-only shell?)")
                failed += 1
                continue

            target.write_text(body, encoding="utf-8")
            manifest[slug] = {
                "url": str(response.url),
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
                "bytes": len(body),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": response.status_code,
            }
            print(f"  OK             {slug:<24} {len(body):>7} bytes")
            fetched += 1

    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total = len(list(FIXTURE_DIR.glob("*.html")))
    print(f"\nfetched={fetched} skipped={skipped} failed={failed}  corpus={total} pages")
    if total < 20:
        print(f"WARNING: Phase 1 wants >= 20 pages, have {total}. Add URLs to {URL_LIST.name}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
