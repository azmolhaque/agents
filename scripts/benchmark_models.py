#!/usr/bin/env python3
"""Phase 1 benchmark. Run this ON THE PI -- numbers from anywhere else are fiction.

    python scripts/benchmark_models.py                  # every configured candidate
    python scripts/benchmark_models.py --model qwen3:4b-instruct
    python scripts/benchmark_models.py --limit 5        # quick smoke run

For each candidate model it runs the extraction prompt over the fixture corpus under a
JSON Schema constraint and records what the Phase 1 gate asks for:

    schema validity %   -- the gate is >= 95%
    p50 / p95 latency   -- per page, end to end
    tokens/sec          -- decode throughput, the number that decides feasibility
    peak SoC temp       -- and whether the throttle flag ever set

Then it writes docs/BENCHMARKS.md. The table is generated, never hand-typed, so it
cannot drift from what was actually measured.

Note what this does NOT measure: field-level accuracy. That needs the hand-labelled
golden set and is the Phase 3 gate. A model can be 100% schema-valid and still be
wrong about everything, which is exactly why the two gates are separate.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cindraleads.config import load_yaml, settings  # noqa: E402
from cindraleads.errors import ConfigError, SchemaValidationError  # noqa: E402
from cindraleads.llm import ModelRegistry, OllamaBackend, StructuredLLM  # noqa: E402
from cindraleads.models import Candidate  # noqa: E402
from cindraleads.textextract import extract_text, selectolax_available  # noqa: E402
from cindraleads.thermal import ThermalGovernor  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "html"
PROMPT_PATH = REPO_ROOT / "prompts" / "extract_company.md"
OUT_PATH = REPO_ROOT / "docs" / "BENCHMARKS.md"


@dataclass
class ModelRun:
    model: str
    pages: int = 0
    valid: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    completion_tokens: list[int] = field(default_factory=list)
    tok_per_sec: list[float] = field(default_factory=list)
    peak_temp_c: float = 0.0
    throttled: bool = False
    errors: list[str] = field(default_factory=list)
    available: bool = True

    @property
    def validity_pct(self) -> float:
        return 100.0 * self.valid / self.pages if self.pages else 0.0

    def pct(self, q: float) -> int:
        if not self.latencies_ms:
            return 0
        ordered = sorted(self.latencies_ms)
        idx = min(len(ordered) - 1, int(q * len(ordered)))
        return ordered[idx]

    @property
    def median_tps(self) -> float:
        return statistics.median(self.tok_per_sec) if self.tok_per_sec else 0.0


def load_fixtures(limit: int | None) -> list[tuple[str, str]]:
    if not FIXTURE_DIR.is_dir():
        sys.exit(
            f"no fixture corpus at {FIXTURE_DIR}\n"
            "Run: python scripts/fetch_fixtures.py   (on a host with open outbound HTTPS)"
        )
    paths = sorted(FIXTURE_DIR.glob("*.html"))
    if limit:
        paths = paths[:limit]
    if not paths:
        sys.exit(f"{FIXTURE_DIR} is empty; run scripts/fetch_fixtures.py first")
    return [(p.stem, p.read_text(encoding="utf-8", errors="replace")) for p in paths]


def candidates(explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    try:
        cfg = load_yaml("models")
    except ConfigError:
        return list(ModelRegistry().roles.values())
    listed = cfg.get("benchmark_candidates") or []
    return [str(m) for m in listed] or list(ModelRegistry().roles.values())


async def run_model(model: str, fixtures: list[tuple[str, str]], template: str) -> ModelRun:
    run = ModelRun(model=model)
    backend = OllamaBackend()
    governor = ThermalGovernor()

    installed = await backend.list_models()
    if model not in installed and f"{model}:latest" not in installed:
        run.available = False
        run.errors.append(f"tag not installed; `ollama pull {model}` (have: {installed})")
        await backend.aclose()
        return run

    # Pin the role so the registry hands back the model under test.
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": model}))

    for slug, html in fixtures:
        text = extract_text(html)
        prompt = template.replace("{url}", f"https://{slug}").replace("{content}", text)
        run.pages += 1
        started = time.monotonic()
        try:
            result = await llm.generate(
                prompt, Candidate, role="workhorse", temperature=0.0, allow_escalation=False
            )
        except SchemaValidationError as exc:
            run.errors.append(f"{slug}: {str(exc)[:160]}")
            run.latencies_ms.append(int((time.monotonic() - started) * 1000))
        else:
            run.valid += 1
            run.latencies_ms.append(result.latency_ms)
            run.completion_tokens.append(result.completion_tokens)
            if result.latency_ms > 0 and result.completion_tokens:
                run.tok_per_sec.append(result.completion_tokens / (result.latency_ms / 1000))

        policy = governor.poll()
        reading = governor.last_reading
        if reading and reading.temp_c:
            run.peak_temp_c = max(run.peak_temp_c, reading.temp_c)
        if reading and reading.throttled_now:
            run.throttled = True
        print(
            f"  {slug:<24} {'ok ' if run.valid else '   '} "
            f"{run.latencies_ms[-1]:>6} ms  state={policy.state}"
        )

    await backend.aclose()
    return run


def render(runs: list[ModelRun], fixture_count: int) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    gate = all(r.validity_pct >= 95 for r in runs if r.available and r.pages)
    lines = [
        "# BENCHMARKS",
        "",
        "**Generated by `scripts/benchmark_models.py` (`make bench`). Do not hand-edit.**",
        "",
        f"- Measured: {stamp}",
        f"- Corpus: {fixture_count} real HTML pages (`tests/fixtures/html/`)",
        f"- Boilerplate stripper: {'selectolax' if selectolax_available() else 'stdlib fallback'}",
        "- Schema: `Candidate`, enforced via Ollama `format`, temperature 0",
        "",
        "## Phase 1 gate: schema validity >= 95%",
        "",
        "| Model | Pages | Schema valid | p50 latency | p95 latency | Median tok/s "
        "| Peak temp | Throttled |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for r in runs:
        if not r.available:
            lines.append(f"| `{r.model}` | — | **NOT INSTALLED** | — | — | — | — | — |")
            continue
        lines.append(
            f"| `{r.model}` | {r.pages} | {r.validity_pct:.1f}% | {r.pct(0.5)} ms | "
            f"{r.pct(0.95)} ms | {r.median_tps:.1f} | "
            f"{r.peak_temp_c:.1f} C | {'YES' if r.throttled else 'no'} |"
        )

    lines += ["", f"**Gate: {'PASS' if gate else 'FAIL'}**", ""]

    unavailable = [r for r in runs if not r.available]
    if unavailable:
        lines += [
            "## Models not installed",
            "",
            "These tags did not resolve on this host. Recorded rather than silently skipped,",
            "because a plan that assumes a model exists is worth less than a table that says",
            "it does not.",
            "",
        ]
        lines += [
            f"- `{r.model}`: {r.errors[0] if r.errors else 'unavailable'}" for r in unavailable
        ]
        lines.append("")

    failures = [(r, e) for r in runs for e in r.errors[:5] if r.available]
    if failures:
        lines += ["## Schema failures (first few per model)", ""]
        lines += [f"- `{r.model}` — {e}" for r, e in failures]
        lines.append("")

    lines += [
        "## What this does not measure",
        "",
        "Field-level accuracy. A model can be 100% schema-valid and wrong about every",
        "field. Accuracy needs the hand-labelled golden set and is the **Phase 3** gate,",
        "split three ways per PLAN.md 2.11: schema validity >= 98%, critical fields",
        "(canonical_domain, display_name, trigger present, evidence URL present) >= 90%,",
        "soft fields (employee_band, industry, funding) >= 70% with unsourced => null.",
        "",
    ]
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings().ensure_dirs()
    fixtures = load_fixtures(args.limit)
    template = PROMPT_PATH.read_text(encoding="utf-8")
    print(f"corpus: {len(fixtures)} pages\n")

    runs: list[ModelRun] = []
    for model in candidates(args.model):
        print(f"--- {model} ---")
        runs.append(await run_model(model, fixtures, template))
        print()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(runs, len(fixtures)), encoding="utf-8")
    print(f"wrote {OUT_PATH}")

    for r in runs:
        if r.available:
            print(f"  {r.model}: {r.validity_pct:.1f}% valid, {r.median_tps:.1f} tok/s")
    usable = [r for r in runs if r.available and r.pages]
    if not usable:
        print("\nNo candidate model was installed. Nothing measured.")
        return 1
    return 0 if all(r.validity_pct >= 95 for r in usable) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", help="benchmark a single tag instead of the configured list")
    ap.add_argument("--limit", type=int, help="use only the first N fixtures")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
