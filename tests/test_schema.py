from __future__ import annotations

from aeview.schema import ReviewOutput, review_output_json_schema


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
