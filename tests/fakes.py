"""Fake model providers for offline synthesis and claims extraction tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from coldscreen.providers import Message


@dataclass
class RecordedCall:
    system: str
    messages: list[Message]
    json_schema: dict[str, Any] | None


@dataclass
class FakeModelProvider:
    """Returns canned responses in order; raises when exhausted."""

    responses: list[str]
    calls: list[RecordedCall] = field(default_factory=list)

    def complete(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append(RecordedCall(system, list(messages), json_schema))
        if not self.responses:
            raise AssertionError("FakeModelProvider ran out of canned responses")
        return self.responses.pop(0)


def synthesis_json(
    level: str = "green",
    triggered: list[str] | None = None,
    narrative: str = "The public record for this company is unremarkable.",
    rationale: str = "No rubric triggers are met by the findings.",
    questions: list[str] | None = None,
    assessments: list[dict[str, Any]] | None = None,
) -> str:
    """A well-formed synthesis output document."""
    return json.dumps(
        {
            "narrative": narrative,
            "verdict": {
                "level": level,
                "triggered": triggered or [],
                "rationale": rationale,
                "questions": questions
                or [
                    "Can you confirm the current trading status of the company?",
                    "Are there any funding arrangements not visible on the register?",
                    "Who currently manages day-to-day operations?",
                ],
            },
            "assessments": assessments or [],
        }
    )


def assessment_json(
    claim_id: str,
    status: str = "unverified",
    basis_finding_ids: list[str] | None = None,
    record_note: str = "No public source in this casefile speaks to this claim.",
) -> dict[str, Any]:
    """One assessment object for the synthesis output document."""
    return {
        "claim_id": claim_id,
        "status": status,
        "basis_finding_ids": basis_finding_ids or [],
        "record_note": record_note,
    }


def claims_json(claims: list[dict[str, Any]] | None = None) -> str:
    """A well-formed claims extraction output document."""
    return json.dumps({"claims": claims or []})


def claim_json(
    text: str,
    source: str,
    category: str = "other",
    checkable: bool = True,
) -> dict[str, Any]:
    """One raw claim object for the claims extraction output document."""
    return {"text": text, "source": source, "category": category, "checkable": checkable}
