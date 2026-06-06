from __future__ import annotations

from aeview.schema import (
    ReviewOutput,
    review_output_json_schema,
    strict_review_output_schema,
)


def test_review_output_round_trips():
    payload = {
        "verdict": "needs-attention",
        "summary": "x",
        "findings": [
            {
                "title": "t",
                "body": "b",
                "severity": "high",
                "category": "bug",
                "confidence": 0.5,
                "location": {"file": "a.py", "line_start": 1, "line_end": 2},
                "recommendation": "fix it",
            }
        ],
        "next_steps": ["s"],
    }
    out = ReviewOutput.model_validate(payload)
    assert out.verdict == "needs-attention"
    assert out.findings[0].location.line_end == 2


def test_json_schema_is_strict():
    schema = review_output_json_schema()
    assert schema["additionalProperties"] is False
    assert "verdict" in schema["required"]


def test_lenient_schema_omits_defaulted_required():
    # The claude (validate-and-reprompt) schema leaves defaulted fields optional.
    schema = review_output_json_schema()
    assert "findings" not in schema["required"]


def test_strict_schema_marks_every_property_required():
    # codex's constrained decoding requires all properties in `required`, recursively.
    schema = strict_review_output_schema()
    assert set(schema["required"]) == {"verdict", "summary", "findings", "next_steps"}
    assert schema["additionalProperties"] is False
    for definition in schema.get("$defs", {}).values():
        if definition.get("type") == "object":
            assert set(definition["required"]) == set(definition["properties"])
            assert definition["additionalProperties"] is False
