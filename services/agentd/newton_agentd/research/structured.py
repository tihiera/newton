"""The JSON shapes Newton asks reading models for, sent as OpenAI-style structured output
(`response_format: json_schema`). Engines that support it (Ollama, vLLM) constrain the
answer to the shape, so a small model can no longer answer with prose, a half object or
a missing field. The prompts still say what each field means, and every answer is still
checked field by field after (parse_card, triage, keywords.clean): the schema is a guide
for the model, not a trust boundary.

List lengths are bounded so an answer fits in its max_tokens and is never cut off.
"""

from __future__ import annotations

from typing import Any

from .papers import CLAIM_KINDS, LIMITER_CHOICES, TIME_CHOICES

CARD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "summary": {"type": "string", "maxLength": 700},
        "method": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 80},
                "limiter": {"type": "string", "enum": list(LIMITER_CHOICES)},
                "second_order_correction": {"type": "boolean"},
                "time_integration": {"type": "string", "enum": list(TIME_CHOICES)},
                "order": {"type": "integer", "minimum": 1, "maximum": 4},
                "max_cfl": {"type": "number"},
                "tvd": {"type": "boolean"},
            },
            "required": [
                "name",
                "limiter",
                "second_order_correction",
                "time_integration",
                "order",
                "max_cfl",
                "tvd",
            ],  # fmt: skip
        },
        "claims": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(CLAIM_KINDS)},
                    "text": {"type": "string", "maxLength": 240},
                },
                "required": ["kind", "text"],
            },
        },
        "benchmarks": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "maxLength": 120},
        },
    },
    "required": ["relevant", "summary", "method", "claims", "benchmarks"],
}

TRIAGE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "why": {"type": "string", "maxLength": 300},
    },
    "required": ["relevant", "why"],
}

KEYWORDS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "minItems": 3,
            "maxItems": 6,
            "items": {"type": "string", "maxLength": 60},
        },
    },
    "required": ["keywords"],
}


# Reading a paper needs no "thinking" first: a thinking model (qwen3, ...) otherwise spends
# a short answer's max_tokens on it and answers nothing. Ollama honours this; the router
# drops it for engines that don't know "none" (vLLM), and MLX never gets it.
NO_THINKING: dict[str, Any] = {"reasoning_effort": "none"}


def response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The `response_format` field of a chat completion asking for `schema`."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}
