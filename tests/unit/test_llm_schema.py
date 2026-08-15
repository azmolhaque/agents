"""The retry -> escalate -> dead-letter ladder.

Backends are fakes, so every rung is exercised deterministically without Ollama.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from cindraleads.errors import ConfigError, SchemaValidationError
from cindraleads.llm import (
    LLMRequest,
    LLMResponse,
    ModelRegistry,
    OllamaBackend,
    StructuredLLM,
)


class Answer(BaseModel):
    name: str
    count: int


class FakeBackend:
    """Replays a scripted list of responses and records what it was asked."""

    def __init__(self, *replies: str | Exception, name: str = "fake") -> None:
        self.name = name
        self._replies = list(replies)
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        reply = self._replies.pop(0) if self._replies else '{"name":"fallback","count":0}'
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(
            text=reply,
            model=request.model,
            backend=self.name,
            latency_ms=10,
            prompt_tokens=100,
            completion_tokens=20,
        )


# ------------------------------------------------------------------ registry


def test_registry_maps_roles_not_tags():
    registry = ModelRegistry({"workhorse": "qwen3:4b-instruct"})
    assert registry.tag("workhorse") == "qwen3:4b-instruct"


def test_registry_rejects_an_unknown_role():
    with pytest.raises(ConfigError, match="unknown model role"):
        ModelRegistry({"workhorse": "x"}).tag("router")


def test_default_registry_has_no_router():
    """PLAN.md 2.2: two resident models, so three cannot contend for two load slots."""
    roles = ModelRegistry().roles
    assert set(roles) == {"workhorse", "embedder"}


# ------------------------------------------------------------- the happy path


async def test_first_attempt_success_does_not_retry():
    backend = FakeBackend('{"name":"acme","count":3}')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}))
    result = await llm.generate("go", Answer)
    assert result.value == Answer(name="acme", count=3)
    assert result.attempts == 1
    assert result.escalated is False
    assert result.cost_units == 0.0
    assert len(backend.requests) == 1


async def test_schema_is_passed_to_the_backend_as_a_grammar_constraint():
    """This is what makes a 4B reliable at JSON. If `format` stops being sent, the
    model is merely being asked nicely and the failure is silent."""
    backend = FakeBackend('{"name":"a","count":1}')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}))
    await llm.generate("go", Answer)
    schema = backend.requests[0].schema
    assert schema is not None
    assert set(schema["properties"]) == {"name", "count"}


async def test_fenced_json_is_recovered():
    backend = FakeBackend('```json\n{"name":"acme","count":2}\n```')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}))
    assert (await llm.generate("go", Answer)).value.count == 2


# ------------------------------------------------------------------ rung 2


async def test_schema_failure_retries_once_at_temperature_zero():
    backend = FakeBackend("I'm sorry, I can't do that.", '{"name":"acme","count":1}')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}))
    result = await llm.generate("go", Answer, temperature=0.7)
    assert result.attempts == 2
    assert result.escalated is False
    assert backend.requests[0].temperature == 0.7
    assert backend.requests[1].temperature == 0.0


async def test_wrong_types_count_as_a_schema_failure():
    backend = FakeBackend('{"name":"acme","count":"three"}', '{"name":"acme","count":3}')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}))
    assert (await llm.generate("go", Answer)).attempts == 2


# ------------------------------------------------------------------ rung 3


async def test_escalation_runs_after_two_local_failures():
    local = FakeBackend("nope", "still nope")
    cloud = FakeBackend('{"name":"acme","count":9}', name="anthropic")
    llm = StructuredLLM(local, registry=ModelRegistry({"workhorse": "m"}), escalator=cloud)
    result = await llm.generate("go", Answer)
    assert result.escalated is True
    assert result.value.count == 9
    assert result.attempts == 3
    assert result.cost_units == 1.0


async def test_escalation_is_skipped_when_the_budget_is_exhausted():
    """Budget exhaustion degrades to local-only. It must never crash and never spend."""
    local = FakeBackend("nope", "nope")
    cloud = FakeBackend('{"name":"x","count":1}', name="anthropic")
    llm = StructuredLLM(
        local,
        registry=ModelRegistry({"workhorse": "m"}),
        escalator=cloud,
        can_escalate=lambda: False,
    )
    with pytest.raises(SchemaValidationError, match="budget exhausted"):
        await llm.generate("go", Answer)
    assert cloud.requests == []


async def test_no_escalator_configured_degrades_to_local_only():
    llm = StructuredLLM(FakeBackend("nope", "nope"), registry=ModelRegistry({"workhorse": "m"}))
    with pytest.raises(SchemaValidationError, match="no escalation backend"):
        await llm.generate("go", Answer)


async def test_caller_can_forbid_escalation_per_call():
    cloud = FakeBackend('{"name":"x","count":1}', name="anthropic")
    llm = StructuredLLM(
        FakeBackend("nope", "nope"),
        registry=ModelRegistry({"workhorse": "m"}),
        escalator=cloud,
    )
    with pytest.raises(SchemaValidationError, match="not permitted"):
        await llm.generate("go", Answer, allow_escalation=False)
    assert cloud.requests == []


async def test_a_failing_cloud_call_still_dead_letters_cleanly():
    local = FakeBackend("nope", "nope")
    cloud = FakeBackend(httpx.ConnectError("cloud down"), name="anthropic")
    llm = StructuredLLM(local, registry=ModelRegistry({"workhorse": "m"}), escalator=cloud)
    with pytest.raises(SchemaValidationError):
        await llm.generate("go", Answer)


# ------------------------------------------------------- transport & gating


async def test_ollama_being_down_does_not_raise_a_transport_error_at_the_caller():
    """Chaos requirement: Ollama down must degrade, not crash-loop. The caller sees
    one SchemaValidationError it knows how to dead-letter."""
    llm = StructuredLLM(
        FakeBackend(httpx.ConnectError("connection refused")),
        registry=ModelRegistry({"workhorse": "m"}),
    )
    with pytest.raises(SchemaValidationError):
        await llm.generate("go", Answer)


async def test_a_transport_error_does_not_burn_the_local_retry():
    """A refused connection will be refused again a millisecond later. Retrying the
    same dead socket wastes the rung that exists for schema flakiness."""
    backend = FakeBackend(httpx.ConnectError("refused"), '{"name":"a","count":1}')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}))
    with pytest.raises(SchemaValidationError):
        await llm.generate("go", Answer)
    assert len(backend.requests) == 1


async def test_thermal_gate_blocks_inference_when_the_pi_is_hot():
    backend = FakeBackend('{"name":"a","count":1}')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}), gate=lambda: False)
    with pytest.raises(SchemaValidationError, match="thermal governor"):
        await llm.generate("go", Answer)
    assert backend.requests == []


# ---------------------------------------------------------------- telemetry


async def test_tokens_per_second_is_computed_for_the_benchmark():
    response = LLMResponse(
        text="{}", model="m", backend="ollama", latency_ms=2000, completion_tokens=50
    )
    assert response.tokens_per_second == 25.0
    assert LLMResponse(text="", model="m", backend="o", latency_ms=0).tokens_per_second == 0.0


async def test_latency_accumulates_across_the_ladder():
    llm = StructuredLLM(
        FakeBackend("nope", '{"name":"a","count":1}'),
        registry=ModelRegistry({"workhorse": "m"}),
    )
    assert (await llm.generate("go", Answer)).latency_ms == 20


def test_ollama_backend_defaults_to_localhost():
    """Nothing outside the Pi should reach the model server."""
    assert OllamaBackend().base_url == "http://localhost:11434"


# ------------------------------------------------------- schema payload size


def test_docstrings_are_not_shipped_to_the_model():
    """Pydantic copies the class docstring into schema['description'].

    On CompanyExtraction that was 59% of the payload, and the text was internal
    rationale the model has no business reading. Field semantics belong in the
    versioned prompt, not smuggled through a docstring.
    """
    from cindraleads.llm import strip_schema_annotations
    from cindraleads.models import CompanyExtraction

    raw = CompanyExtraction.model_json_schema()
    assert "description" in raw, "precondition: pydantic emits the docstring"

    stripped = strip_schema_annotations(raw)
    assert "description" not in stripped
    assert "title" not in stripped
    # Properties keep their structure; only annotations go.
    assert set(stripped["properties"]) == set(raw["properties"])
    assert stripped["properties"]["display_name"]["type"] == "string"


async def test_the_schema_sent_to_the_backend_is_stripped():
    backend = FakeBackend('{"name":"a","count":1}')
    llm = StructuredLLM(backend, registry=ModelRegistry({"workhorse": "m"}))
    await llm.generate("go", Answer)
    sent = backend.requests[0].schema
    assert sent is not None
    assert "title" not in sent
    assert set(sent["properties"]) == {"name", "count"}


def test_lean_extraction_schema_has_no_nested_defs():
    """Ollama compiles the schema to a sampling grammar; nested $defs make it
    large and slow constrained decoding on a Pi."""
    from cindraleads.models import Candidate, CompanyExtraction

    assert CompanyExtraction.model_json_schema().get("$defs", {}) == {}
    assert Candidate.model_json_schema().get("$defs", {}), "Candidate is the nested one"


def test_extraction_model_cannot_carry_an_evidence_url():
    """The model must never emit a URL or content hash: those are facts the
    Harvester already holds, and a fabricated one poisons the evidence rule."""
    from cindraleads.models import CompanyExtraction

    fields = set(CompanyExtraction.model_fields)
    assert not {"url", "content_sha256", "evidence", "source"} & fields
    assert "evidence_snippets" in fields


def test_stripper_keeps_a_field_that_is_literally_named_description():
    """Regression: CompanyExtraction has a `description` FIELD. A naive filter that
    drops every dict key named "description" deletes it from `properties`, and the
    model silently stops being asked for it."""
    from cindraleads.llm import strip_schema_annotations

    schema = {
        "title": "Thing",
        "description": "the docstring, must go",
        "properties": {
            "description": {"type": "string", "description": "annotation, must go"},
            "title": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["description"],
    }
    out = strip_schema_annotations(schema)
    assert "description" not in out
    assert set(out["properties"]) == {"description", "title", "name"}
    assert out["properties"]["description"] == {"type": "string"}
    assert out["required"] == ["description"]
