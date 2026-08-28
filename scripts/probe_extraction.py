#!/usr/bin/env python
"""Ask the same page three times under three grammars, and print what came back raw.

`description` and `industry` have been null for every company ever extracted --
583 of 583, and 0 of 37 after the prompt was fixed to describe them. Two prompt
hypotheses have now been written and both missed, so this stops arguing about the
wording and looks at the one thing nothing in the pipeline records: **the bytes the
model actually returned**.

`StructuredLLM.generate` parses and discards the raw text, so an omitted key and an
explicit `null` are indistinguishable by the time anything logs them -- Pydantic turns
both into `None`. They are completely different defects:

  * **omitted** -- the grammar let the model skip the field. `required` is
    `['display_name']` and nothing else, so every optional property is legal to leave
    out, and the shortest legal completion is the one a low-temperature sampler takes.
    That is a schema bug and no amount of prompt wording fixes it.
  * **explicit null** -- the model was asked, considered, and declined. That is a
    prompt or a page-budget problem, and the next move is rule wording or
    `PROMPT_CHAR_BUDGET`.

The three variants differ *only* in the grammar. The prompt is byte-identical in all
three, so whatever the difference is, it is not the wording:

  control   the shipping schema, exactly as the Extractor sends it
  required  same, but `description`/`industry` are required and non-nullable, so the
            grammar has no path that omits them and no `null` branch to fall into
  minimal   three fields only. If the model fills it here and not in `control`, the
            cost is the size of the object rather than the fields themselves

Run it on the Pi -- it needs the real Ollama:

    .venv/bin/python scripts/probe_extraction.py https://rtrvr.ai

Roughly 64 s per variant per URL. It writes nothing to the database.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from typing import Any

import httpx

from cindraleads.agents.extractor import PROMPT_CHAR_BUDGET
from cindraleads.config import load_prompt, settings
from cindraleads.injection import wrap_untrusted
from cindraleads.llm import LLMRequest, ModelRegistry, OllamaBackend, strip_schema_annotations
from cindraleads.models import CompanyExtraction
from cindraleads.textextract import extract_text

PROBED_FIELDS = ("description", "industry")


def _control_schema() -> dict[str, Any]:
    schema = strip_schema_annotations(CompanyExtraction.model_json_schema())
    assert isinstance(schema, dict)
    return schema


def _required_schema() -> dict[str, Any]:
    """The shipping schema with the two fields' `null` branch and optionality removed.

    `str | None` with a default becomes `anyOf: [{string}, {null}]` plus absence from
    `required`, which is two separate invitations to say nothing. This variant takes
    the string branch alone and puts the key in `required`, leaving the model no legal
    output that lacks a value.
    """
    schema = copy.deepcopy(_control_schema())
    props = schema["properties"]
    for name in PROBED_FIELDS:
        branches = props[name].get("anyOf", [])
        string_branch = next((b for b in branches if b.get("type") == "string"), None)
        if string_branch is None:  # already a bare string -- the fix has landed
            continue
        props[name] = dict(string_branch)
    schema["required"] = sorted({*schema.get("required", []), *PROBED_FIELDS})
    return schema


def _minimal_schema() -> dict[str, Any]:
    """Name plus the two fields under investigation. Nothing else to spend decode on."""
    control = _control_schema()["properties"]
    return {
        "type": "object",
        "properties": {
            "display_name": dict(control["display_name"]),
            **{
                name: next(b for b in control[name]["anyOf"] if b.get("type") == "string")
                for name in PROBED_FIELDS
            },
        },
        "required": ["display_name", *PROBED_FIELDS],
    }


def _verdict(raw: str) -> str:
    """Omitted, explicitly null, or filled -- per field, from the raw bytes."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return "unparseable JSON"
    if not isinstance(obj, dict):
        return "not an object"
    parts = []
    for name in PROBED_FIELDS:
        if name not in obj:
            parts.append(f"{name}=OMITTED")
        elif obj[name] is None:
            parts.append(f"{name}=null")
        else:
            parts.append(f"{name}={obj[name]!r}")
    return "  ".join(parts)


async def _probe(url: str, *, model: str, backend: OllamaBackend, template: str) -> None:
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": "cindraleads-probe"}
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        body = response.text

    page_text = extract_text(body, max_chars=PROMPT_CHAR_BUDGET)
    prompt = template.format(url=url, content=wrap_untrusted(page_text))
    print(f"\n=== {url}")
    print(f"page text fed to the model: {len(page_text)} chars (budget {PROMPT_CHAR_BUDGET})")

    for name, schema in (
        ("control", _control_schema()),
        ("required", _required_schema()),
        ("minimal", _minimal_schema()),
    ):
        request = LLMRequest(
            prompt=prompt, schema=schema, model=model, temperature=0.2, max_tokens=1024
        )
        try:
            result = await backend.generate(request)
        except (httpx.HTTPError, OSError) as exc:
            print(f"  {name:9} BACKEND ERROR {type(exc).__name__}: {exc}")
            continue
        print(f"  {name:9} {result.latency_ms / 1000:5.1f}s  {_verdict(result.text)}")
        print(f"            raw: {result.text.strip()[:600]}")


async def _main(urls: list[str]) -> int:
    cfg = settings()
    template = load_prompt("extract_company", base=cfg.resolve(cfg.prompt_dir))
    model = ModelRegistry.from_config(cfg).tag("workhorse")
    backend = OllamaBackend()
    print(f"model: {model}")
    for url in urls:
        try:
            await _probe(url, model=model, backend=backend, template=template)
        except (httpx.HTTPError, OSError) as exc:
            print(f"\n=== {url}\n  FETCH FAILED {type(exc).__name__}: {exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+", help="page URLs to extract")
    args = parser.parse_args()
    return asyncio.run(_main(args.urls))


if __name__ == "__main__":
    sys.exit(main())
