"""Local-first LLM calls with schema-constrained output.

Two rules from the project charter shape this module:

**Structured output is mandatory.** Every call passes a JSON Schema through Ollama's
``format`` parameter and the result is parsed into a Pydantic model. We never regex a
model's prose into a field. A 4B model asked politely for JSON will occasionally answer
with an apology; a 4B model constrained by a grammar cannot.

**Cloud is a rationed escalation path, not the default.** The ladder is:

    1. local model, at the caller's temperature
    2. local model again at temperature 0        (schema failure only)
    3. cloud escalation                          (if configured AND permitted)
    4. SchemaValidationError                     -> the caller dead-letters the job

Step 3 is optional at every level: no key, no budget, or no escalator configured all
degrade to local-only with a warning rather than crashing. That is a hard requirement -
the pipeline must keep producing leads when the internet or the budget is gone.

Backends are injected, so the whole ladder is testable without Ollama running.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.errors import ConfigError, SchemaValidationError
from cindraleads.logging import get_logger

__all__ = [
    "LLMBackend",
    "LLMRequest",
    "LLMResponse",
    "ModelRegistry",
    "OllamaBackend",
    "StructuredLLM",
    "StructuredResult",
    "strip_schema_annotations",
]

T = TypeVar("T", bound=BaseModel)

log = get_logger("cindraleads.llm")

# Roles, not tags. Swapping a model is a config edit, never a code edit.
DEFAULT_ROLES: dict[str, str] = {
    # PLAN.md 2.2: no separate router. Three models contend for two Ollama load slots,
    # so the workhorse answers binary classification itself in ~5 constrained tokens.
    "workhorse": "qwen3:4b-instruct",
    "embedder": "bge-m3",
}


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    schema: dict[str, Any] | None = None
    system: str | None = None
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 1024
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    backend: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens_per_second(self) -> float:
        if self.latency_ms <= 0 or not self.completion_tokens:
            return 0.0
        return self.completion_tokens / (self.latency_ms / 1000)


class LLMBackend(Protocol):
    """Anything that can turn a request into text. Ollama, Anthropic, or a fake."""

    name: str

    async def generate(self, request: LLMRequest) -> LLMResponse: ...


@dataclass
class StructuredResult(Generic[T]):
    """A parsed answer plus everything needed to bill and debug it."""

    value: T
    model: str
    backend: str
    attempts: int
    escalated: bool
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def cost_units(self) -> float:
        """Cloud calls cost money; local calls cost heat. Only the former is billed."""
        return 1.0 if self.escalated else 0.0


# --------------------------------------------------------------------- registry


class ModelRegistry:
    """Maps a role to a concrete Ollama tag, from ``config/models.yaml``."""

    def __init__(self, roles: dict[str, str] | None = None) -> None:
        self.roles = dict(roles or DEFAULT_ROLES)

    @classmethod
    def from_config(cls, config: Settings | None = None) -> ModelRegistry:
        cfg = config or settings()
        try:
            data = load_yaml("models", base=cfg.resolve(cfg.config_dir))
        except ConfigError:
            log.warning("models_yaml_missing", fallback=sorted(DEFAULT_ROLES))
            return cls()
        raw = data.get("roles", {})
        if not isinstance(raw, dict) or not raw:
            raise ConfigError("config/models.yaml needs a non-empty 'roles' mapping")
        return cls({str(k): str(v) for k, v in raw.items()})

    def tag(self, role: str) -> str:
        try:
            return self.roles[role]
        except KeyError:
            raise ConfigError(
                f"unknown model role {role!r}; configured roles: {sorted(self.roles)}"
            ) from None


# --------------------------------------------------------------------- backends


class OllamaBackend:
    """Talks to a local Ollama daemon."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def generate(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
                **request.options,
            },
        }
        if request.system:
            payload["system"] = request.system
        if request.schema is not None:
            # The grammar constraint. This is what makes a 4B reliable at JSON.
            payload["format"] = request.schema

        client = await self._http()
        started = time.monotonic()
        response = await client.post(f"{self.base_url}/api/generate", json=payload)
        response.raise_for_status()
        body = response.json()
        return LLMResponse(
            text=body.get("response", ""),
            model=request.model,
            backend=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=int(body.get("prompt_eval_count", 0)),
            completion_tokens=int(body.get("eval_count", 0)),
        )

    async def list_models(self) -> list[str]:
        client = await self._http()
        response = await client.get(f"{self.base_url}/api/tags")
        response.raise_for_status()
        return [str(m.get("name", "")) for m in response.json().get("models", [])]

    async def healthy(self) -> bool:
        try:
            await self.list_models()
        except (httpx.HTTPError, ValueError):
            return False
        return True

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ------------------------------------------------------------------ orchestrator


_ANNOTATION_KEYWORDS = frozenset({"description", "title"})
# Maps whose keys are field names rather than schema keywords.
_NAME_KEYED_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


def strip_schema_annotations(node: Any) -> Any:
    """Remove ``description`` and ``title`` from a JSON Schema, recursively.

    Pydantic copies a model's docstring into ``description`` and its class name into
    ``title``. That is useful for OpenAPI and pure overhead here: measured on
    ``CompanyExtraction`` it was 59% of the schema payload, and the text was internal
    engineering rationale ("hallucination surface", "measured on a Pi 5") that the
    model has no business reading.

    Field semantics belong in the prompt, which is versioned and reviewed, not
    smuggled in through docstrings that nobody realises are being sent.
    """
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in _ANNOTATION_KEYWORDS:
                continue
            if key in _NAME_KEYED_MAPS and isinstance(value, dict):
                # The keys inside `properties` / `$defs` are user-chosen FIELD NAMES,
                # not schema keywords. CompanyExtraction has a field called
                # `description`; filtering by key here would delete it from the
                # schema and the model would silently stop being asked for it.
                result[key] = {name: strip_schema_annotations(sub) for name, sub in value.items()}
            else:
                result[key] = strip_schema_annotations(value)
        return result
    if isinstance(node, list):
        return [strip_schema_annotations(item) for item in node]
    return node


def _extract_json(text: str) -> str:
    """Recover the JSON object from a response with stray prose around it.

    With ``format`` set this should never be needed. It exists because a model that has
    been asked to think out loud may still wrap the object in a fenced block, and
    throwing away an otherwise-valid answer over a code fence is wasteful. It is
    deliberately narrow: it slices between the outermost braces and does nothing
    cleverer. This is not "regex the prose into a field" - the result still has to
    survive full Pydantic validation.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        if stripped.lstrip().startswith("json"):
            stripped = stripped.lstrip()[4:]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


class StructuredLLM:
    """The retry -> escalate -> dead-letter ladder."""

    def __init__(
        self,
        backend: LLMBackend,
        *,
        registry: ModelRegistry | None = None,
        escalator: LLMBackend | None = None,
        escalation_model: str = "claude-haiku-4-5",
        can_escalate: Callable[[], bool] | None = None,
        gate: Callable[[], bool] | None = None,
    ) -> None:
        self.backend = backend
        self.registry = registry or ModelRegistry()
        self.escalator = escalator
        self.escalation_model = escalation_model
        # Budget guard (Phase 2) and thermal governor plug in here without this module
        # needing to know either of them exists.
        self._can_escalate = can_escalate or (lambda: True)
        self._gate = gate or (lambda: True)

    def _parse(self, text: str, model_cls: type[T]) -> T:
        try:
            return model_cls.model_validate_json(_extract_json(text))
        except (ValidationError, ValueError) as exc:
            raise SchemaValidationError(str(exc)) from exc

    async def generate(
        self,
        prompt: str,
        model_cls: type[T],
        *,
        role: str = "workhorse",
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        allow_escalation: bool = True,
    ) -> StructuredResult[T]:
        """Return a validated ``model_cls``, or raise :class:`SchemaValidationError`."""
        if not self._gate():
            raise SchemaValidationError(
                "LLM inference is paused by the thermal governor; retry when cool"
            )

        schema = strip_schema_annotations(model_cls.model_json_schema())
        tag = self.registry.tag(role)
        attempts = 0
        total_ms = 0
        failures: list[str] = []

        # Rungs 1 and 2: local, then local again pinned to temperature 0.
        for temp in (temperature, 0.0):
            attempts += 1
            request = LLMRequest(
                prompt=prompt,
                schema=schema,
                system=system,
                model=tag,
                temperature=temp,
                max_tokens=max_tokens,
            )
            try:
                response = await self.backend.generate(request)
            except (httpx.HTTPError, OSError) as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
                log.warning("llm_backend_error", role=role, model=tag, error=str(exc))
                break
            total_ms += response.latency_ms
            try:
                parsed = self._parse(response.text, model_cls)
            except SchemaValidationError as exc:
                failures.append(str(exc))
                log.warning(
                    "llm_schema_invalid", role=role, model=tag, attempt=attempts, temperature=temp
                )
                continue
            return StructuredResult(
                value=parsed,
                model=tag,
                backend=response.backend,
                attempts=attempts,
                escalated=False,
                latency_ms=total_ms,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            # Only a schema failure reaches the next iteration.

        # Rung 3: cloud. Every reason to skip it is a degrade, never a crash.
        if not allow_escalation:
            reason = "escalation not permitted for this call"
        elif self.escalator is None:
            reason = "no escalation backend configured"
        elif not self._can_escalate():
            reason = "escalation budget exhausted"
        else:
            attempts += 1
            try:
                response = await self.escalator.generate(
                    LLMRequest(
                        prompt=prompt,
                        schema=schema,
                        system=system,
                        model=self.escalation_model,
                        temperature=0.0,
                        max_tokens=max_tokens,
                    )
                )
                total_ms += response.latency_ms
                parsed = self._parse(response.text, model_cls)
            except (httpx.HTTPError, OSError, SchemaValidationError) as exc:
                failures.append(f"escalation: {exc}")
                log.error("llm_escalation_failed", role=role, error=str(exc))
            else:
                log.info("llm_escalated", role=role, model=self.escalation_model)
                return StructuredResult(
                    value=parsed,
                    model=self.escalation_model,
                    backend=response.backend,
                    attempts=attempts,
                    escalated=True,
                    latency_ms=total_ms,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )
            reason = "escalation attempt failed"

        log.warning("llm_degraded_local_only", role=role, reason=reason)

        # Rung 4: give up cleanly so the caller can dead-letter with the evidence.
        raise SchemaValidationError(
            f"{model_cls.__name__} could not be produced after {attempts} attempt(s) "
            f"({reason}); failures: {json.dumps(failures[:3])}"
        )
