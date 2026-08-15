# Fixtures

## `html/` — the Phase 1 corpus (currently empty)

`html/` holds real, saved HTML pages. It is empty in this checkout and **the pages are
not committed yet**.

### Why

They were meant to be gathered during the Phase 1 build, but this repository's build
container routes outbound HTTPS through a policy proxy that answers `403` to `CONNECT`
for general web hosts:

```
$ curl -sS -o /dev/null -w '%{http_code}' https://posthog.com/
curl: (56) CONNECT tunnel failed, response 403
```

Only a small allowlist (pypi.org, files.pythonhosted.org, raw.githubusercontent.com,
the npm/crates/go registries) is reachable. That is enough to install dependencies and
push code, and not enough to collect a corpus of startup marketing pages.

Rather than fabricate pages and call them real — which would make the Phase 1 schema-
validity number meaningless — the corpus is gathered by a script that runs where the
network is open.

### How to populate it

On the Pi, or any machine with normal outbound HTTPS:

```bash
python scripts/fetch_fixtures.py          # fetch anything missing
python scripts/fetch_fixtures.py --force  # re-fetch everything
```

The URL set lives in `urls.txt`, one `slug<TAB>url` per line, so the corpus is
reviewable in a diff. The fetcher obeys the same rules the Phase 2 Harvester will:
`robots.txt` parsed per origin, a User-Agent naming Cindrasec with a contact address,
at least 3 seconds between requests to the same host, public pages only.

Each page's URL, sha256 and byte count are written to `manifest.json`, so a later run
can prove the corpus has not silently drifted under the golden tests.

### What makes a good fixture

`urls.txt` mixes three shapes on purpose:

- **homepages** — the common case: heavy JS shells with sparse extractable prose
- **about pages** — headcount, location, founding date
- **careers pages** — where the `T3` / `T4` / `T11` hiring triggers actually live

It also includes two established security vendors as **anti-ICP negatives**. The
extractor must still describe them correctly; it is the ComplianceGate's job in Phase 5
to reject them. A corpus made only of good prospects cannot catch a scorer that likes
everything.

### What the corpus is and is not used for

| Phase | Use | Needs labels? |
| --- | --- | --- |
| 1 | Schema validity >= 95% under Ollama `format` | No |
| 3 | Field accuracy against a 50-item hand-labelled golden set | **Yes** |

Phase 1 only needs realistic HTML. The hand-labelling — and the three-way accuracy
split in PLAN.md 2.11 — comes in Phase 3.
