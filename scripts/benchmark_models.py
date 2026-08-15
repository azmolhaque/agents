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
from cindraleads.models import Candidate, CompanyExtraction  # noqa: E402
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
    prompt_tokens: list[int] = field(default_factory=list)
    prompt_eval_ms: list[int] = field(default_factory=list)
    eval_ms: list[int] = field(default_factory=list)
    load_ms: list[int] = field(default_factory=list)
    decode_tps: list[float] = field(default_factory=list)
    prefill_tps: list[float] = field(default_factory=list)
    timeouts: int = 0
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

    @property
    def median_decode_tps(self) -> float:
        return statistics.median(self.decode_tps) if self.decode_tps else 0.0

    @property
    def median_prefill_tps(self) -> float:
        return statistics.median(self.prefill_tps) if self.prefill_tps else 0.0

    @property
    def median_prefill_ms(self) -> int:
        return int(statistics.median(self.prompt_eval_ms)) if self.prompt_eval_ms else 0

    @property
    def median_decode_ms(self) -> int:
        return int(statistics.median(self.eval_ms)) if self.eval_ms else 0


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


async def run_model(
    model: str,
    fixtures: list[tuple[str, str]],
    template: str,
    *,
    schema_cls: type,
    max_chars: int,
    timeout: float,
    max_tokens: int,
) -> ModelRun:
    run = ModelRun(model=model)
    backend = OllamaBackend(timeout=timeout)
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
        text = extract_text(html, max_chars=max_chars)
        prompt = template.replace("{url}", f"https://{slug}").replace("{content}", text)
        run.pages += 1
        started = time.monotonic()
        page_ok = False
        try:
            result = await llm.generate(
                prompt,
                schema_cls,
                role="workhorse",
                temperature=0.0,
                max_tokens=max_tokens,
                allow_escalation=False,
            )
        except SchemaValidationError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            run.latencies_ms.append(elapsed)
            # A timeout is a capacity problem; a parse failure is a prompt/model
            # problem. Lumping them together hides which one you actually have.
            if elapsed >= timeout * 1000 * 0.95 or "Timeout" in str(exc):
                run.timeouts += 1
                run.errors.append(f"{slug}: TIMEOUT after {elapsed} ms")
            else:
                run.errors.append(f"{slug}: {str(exc)[:160]}")
        else:
            page_ok = True
            run.valid += 1
            run.latencies_ms.append(result.latency_ms)
            run.completion_tokens.append(result.completion_tokens)
            run.prompt_tokens.append(result.prompt_tokens)
            if result.latency_ms > 0 and result.completion_tokens:
                run.tok_per_sec.append(result.completion_tokens / (result.latency_ms / 1000))
            if result.prompt_eval_ms:
                run.prompt_eval_ms.append(result.prompt_eval_ms)
                run.prefill_tps.append(result.prefill_tokens_per_second)
            if result.eval_ms:
                run.eval_ms.append(result.eval_ms)
                run.decode_tps.append(result.decode_tokens_per_second)
            run.load_ms.append(result.load_ms)

        policy = governor.poll()
        reading = governor.last_reading
        if reading and reading.temp_c:
            run.peak_temp_c = max(run.peak_temp_c, reading.temp_c)
        if reading and reading.throttled_now:
            run.throttled = True
        # page_ok, not run.valid: the old version printed "ok" for every page after
        # the first success, because a running total is always truthy.
        split = ""
        if page_ok and run.prompt_eval_ms and run.eval_ms:
            split = f" [prefill {run.prompt_eval_ms[-1]:>6}ms + decode {run.eval_ms[-1]:>6}ms"
            # Cold model load is a storage cost, not a model cost. On microSD a 3.2 GB
            # load is ~30 s and lands entirely on the first page of a run.
            if run.load_ms and run.load_ms[-1] > 500:
                split += f" + load {run.load_ms[-1]:>6}ms"
            split += "]"
        print(
            f"  {slug:<24} {'ok  ' if page_ok else 'FAIL'} "
            f"{run.latencies_ms[-1]:>7} ms  {policy.state:<9} "
            f"{'' if not reading or reading.temp_c is None else f'{reading.temp_c:.1f}C'}"
            f"{split}"
        )

    await backend.aclose()
    return run


def render(
    runs: list[ModelRun],
    fixture_count: int,
    *,
    schema_name: str = "CompanyExtraction",
    max_chars: int = 0,
    max_tokens: int = 0,
    timeout: float = 0.0,
) -> str:
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
        f"- Schema: `{schema_name}`, enforced via Ollama `format`, temperature 0",
        f"- Prompt budget: {max_chars} chars/page, num_predict={max_tokens}, timeout={timeout}s",
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
            f"| `{r.model}` | {r.pages} | {r.validity_pct:.1f}%"
            + (f" ({r.timeouts} timeout)" if r.timeouts else "")
            + f" | {r.pct(0.5)} ms | "
            f"{r.pct(0.95)} ms | {r.median_tps:.1f} | "
            f"{r.peak_temp_c:.1f} C | {'YES' if r.throttled else 'no'} |"
        )

    lines += ["", f"**Gate: {'PASS' if gate else 'FAIL'}**", ""]

    usable = [r for r in runs if r.available and r.valid]
    if usable:
        lines += [
            "## Where the time goes",
            "",
            "Prompt eval (reading the page) and decode (writing the JSON) have different",
            "fixes: shorten the input vs shrink the output. A single tok/s figure over",
            "total latency hides which one is binding.",
            "",
            "| Model | Median prefill | Median decode | Prefill tok/s | Decode tok/s | Cold load |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for r in usable:
            lines.append(
                f"| `{r.model}` | {r.median_prefill_ms} ms | {r.median_decode_ms} ms | "
                f"{r.median_prefill_tps:.1f} | {r.median_decode_tps:.1f} | "
                f"{max(r.load_ms) if r.load_ms else 0} ms |"
            )
        lines.append("")

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
    schema_cls = CompanyExtraction if args.schema == "lean" else Candidate
    print(
        f"corpus: {len(fixtures)} pages | schema={args.schema} ({schema_cls.__name__}) "
        f"| max_chars={args.max_chars} | max_tokens={args.max_tokens} "
        f"| timeout={args.timeout}s\n"
    )

    runs: list[ModelRun] = []
    for model in candidates(args.model):
        print(f"--- {model} ---")
        runs.append(
            await run_model(
                model,
                fixtures,
                template,
                schema_cls=schema_cls,
                max_chars=args.max_chars,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
            )
        )
        print()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        render(
            runs,
            len(fixtures),
            schema_name=schema_cls.__name__,
            max_chars=args.max_chars,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_PATH}")

    for r in runs:
        if r.available:
            print(
                f"  {r.model}: {r.validity_pct:.1f}% valid, {r.median_tps:.1f} tok/s, "
                f"{r.timeouts} timeout(s), peak {r.peak_temp_c:.1f}C"
            )
    usable = [r for r in runs if r.available and r.pages]
    if not usable:
        print("\nNo candidate model was installed. Nothing measured.")
        return 1
    return 0 if all(r.validity_pct >= 95 for r in usable) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", help="benchmark a single tag instead of the configured list")
    ap.add_argument("--limit", type=int, help="use only the first N fixtures")
    ap.add_argument(
        "--schema",
        choices=["lean", "full"],
        default="lean",
        help="lean=CompanyExtraction (flat, no nested $defs); full=Candidate",
    )
    ap.add_argument(
        "--max-chars",
        type=int,
        default=1500,
        help="prompt text budget per page; measured 2.3x faster than 4000 on a Pi 5",
    )
    ap.add_argument("--timeout", type=float, default=240.0, help="per-request timeout, seconds")
    # 768 tokens at the ~3.3 tok/s measured on a Pi 5 is 233 s, which is longer than
    # the request timeout: a full-length answer literally cannot finish. 320 bounds
    # the worst case to ~97 s and is ample for a CompanyExtraction object.
    # Must exceed the worst case the schema itself permits, or a maximally-detailed
    # page gets truncated mid-JSON and fails validation. See the invariant test in
    # tests/unit/test_models.py.
    ap.add_argument("--max-tokens", type=int, default=400, help="num_predict cap")
    return ap


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
