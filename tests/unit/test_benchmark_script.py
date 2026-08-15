"""Coverage for scripts/benchmark_models.py.

This script only ever runs on the Pi, which made it the one piece of code with no
tests — and it promptly shipped two bugs to real hardware: an "ok" marker driven by a
running total, and reading timing fields off the wrong class. Both would have been
caught here in under a second.

The script is loaded by path (it is not an installed module) and driven with a fake
backend, so these tests need neither Ollama nor a Pi.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "benchmark_models.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_models", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_models"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    return _load_script()


class FakeOllama:
    """Stands in for OllamaBackend, including the timing fields Ollama reports."""

    name = "ollama"

    def __init__(self, *_args, fail_slugs: set[str] | None = None, **_kwargs) -> None:
        self.fail_slugs = fail_slugs or set()
        self.calls: list[object] = []

    async def list_models(self) -> list[str]:
        return ["qwen3:4b-instruct", "bge-m3"]

    async def generate(self, request):  # type: ignore[no-untyped-def]
        from cindraleads.llm import LLMResponse

        self.calls.append(request)
        broken = any(slug in request.prompt for slug in self.fail_slugs)
        payload = "not json at all" if broken else json.dumps({"display_name": "Acme"})
        return LLMResponse(
            text=payload,
            model=request.model,
            backend=self.name,
            latency_ms=52_000,
            prompt_tokens=1100,
            completion_tokens=172,
            prompt_eval_ms=31_000,
            eval_ms=20_500,
            load_ms=10,
        )

    async def aclose(self) -> None:
        return None


def _run(bench: ModuleType, fixtures, **kwargs):  # type: ignore[no-untyped-def]
    from cindraleads.models import CompanyExtraction

    params = {
        "schema_cls": CompanyExtraction,
        "max_chars": 4000,
        "timeout": 180.0,
        "max_tokens": 320,
    }
    params.update(kwargs)
    return asyncio.run(
        bench.run_model("qwen3:4b-instruct", fixtures, "URL {url}\n{content}", **params)
    )


def test_timing_split_is_recorded(bench: ModuleType, monkeypatch):
    """The regression that broke on the Pi: StructuredResult must carry the
    prefill/decode split, not just LLMResponse."""
    monkeypatch.setattr(bench, "OllamaBackend", FakeOllama)
    monkeypatch.setattr(bench, "ThermalGovernor", lambda *a, **k: _NullGovernor())

    run = _run(bench, [("acme_home", "<p>Acme ships an AI assistant</p>")])

    assert run.valid == 1
    assert run.prompt_eval_ms == [31_000]
    assert run.eval_ms == [20_500]
    assert run.median_decode_tps == pytest.approx(172 / 20.5, rel=0.01)
    assert run.median_prefill_tps == pytest.approx(1100 / 31.0, rel=0.01)
    # End-to-end is much lower than decode alone; that gap is the whole point.
    assert run.median_tps < run.median_decode_tps


def test_failure_is_reported_per_page_not_by_running_total(bench: ModuleType, monkeypatch, capsys):
    """The other Pi bug: 'ok' was printed whenever the running valid count was
    truthy, so every page after the first success looked successful."""
    monkeypatch.setattr(bench, "OllamaBackend", lambda *a, **k: FakeOllama(fail_slugs={"bad_page"}))
    monkeypatch.setattr(bench, "ThermalGovernor", lambda *a, **k: _NullGovernor())

    run = _run(
        bench,
        [
            ("good_page", "<p>fine</p>"),
            ("bad_page", "<p>fine</p>"),
            ("good_page_2", "<p>fine</p>"),
        ],
    )
    out = capsys.readouterr().out

    assert run.pages == 3
    assert run.valid == 2
    assert "FAIL" in out, "the failing page must be visibly marked"
    assert out.count("ok  ") == 2


def test_timeouts_are_counted_separately_from_parse_failures(bench: ModuleType, monkeypatch):
    """A timeout is a capacity problem, a parse failure is a prompt problem.
    Merging them hides which one you actually have."""
    monkeypatch.setattr(bench, "OllamaBackend", lambda *a, **k: FakeOllama(fail_slugs={"bad_page"}))
    monkeypatch.setattr(bench, "ThermalGovernor", lambda *a, **k: _NullGovernor())

    run = _run(bench, [("bad_page", "<p>x</p>")], timeout=180.0)
    assert run.valid == 0
    assert run.timeouts == 0, "a fast parse failure is not a timeout"
    assert any("TIMEOUT" not in e for e in run.errors)


def test_unavailable_model_is_recorded_not_skipped(bench: ModuleType, monkeypatch):
    class NoModels(FakeOllama):
        async def list_models(self) -> list[str]:
            return ["something-else"]

    monkeypatch.setattr(bench, "OllamaBackend", NoModels)
    monkeypatch.setattr(bench, "ThermalGovernor", lambda *a, **k: _NullGovernor())

    run = _run(bench, [("acme", "<p>x</p>")])
    assert run.available is False
    assert "not installed" in run.errors[0]


def test_render_produces_a_table_with_the_gate_verdict(bench: ModuleType):
    run = bench.ModelRun(model="m", pages=10, valid=10)
    run.latencies_ms = [1000] * 10
    run.tok_per_sec = [3.3] * 10
    run.decode_tps = [8.4] * 10
    run.prefill_tps = [35.5] * 10
    run.prompt_eval_ms = [31000] * 10
    run.eval_ms = [20500] * 10

    out = bench.render([run], 10)
    assert "**Gate: PASS**" in out
    assert "Where the time goes" in out
    assert "100.0%" in out

    run.valid = 5
    assert "**Gate: FAIL**" in bench.render([run], 10)


def test_default_token_cap_cannot_guarantee_a_timeout(bench: ModuleType):
    """768 tokens at the 3.3 tok/s measured on a Pi 5 is 233 s -- longer than the
    180 s timeout, so a full-length answer could never finish. That was a real bug
    that burned a run on real hardware, so the invariant is pinned here against the
    script's actual defaults rather than a copy of them."""
    defaults = {a.dest: a.default for a in bench.build_parser()._actions}
    max_tokens = defaults["max_tokens"]
    timeout = defaults["timeout"]

    measured_pi_tok_per_sec = 3.3
    worst_case_seconds = max_tokens / measured_pi_tok_per_sec
    assert worst_case_seconds < timeout, (
        f"num_predict={max_tokens} needs {worst_case_seconds:.0f}s at Pi speed "
        f"but the timeout is {timeout}s: every full-length answer would be cut off"
    )


def test_default_prompt_budget_is_pi_sized(bench: ModuleType):
    """12,000 chars timed out every page on the Pi. Keep the default modest."""
    defaults = {a.dest: a.default for a in bench.build_parser()._actions}
    assert defaults["max_chars"] <= 6000
    assert defaults["schema"] == "lean"


class _NullGovernor:
    """No sensors in CI; the benchmark must still run."""

    def poll(self):  # type: ignore[no-untyped-def]
        from cindraleads.thermal import ThermalPolicy

        return ThermalPolicy(
            state="nominal",
            max_workers=4,
            allow_llm=True,
            allow_llm_batch=True,
            alert_level="none",
            reason="test",
        )

    @property
    def last_reading(self):  # type: ignore[no-untyped-def]
        return None
