"""Schema enforcement tests. The core one: no Finding without evidence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from firstpass.models import (
    RESEARCH_AID_DISCLAIMER,
    CaseFile,
    CompanyProfile,
    Evidence,
    FilingSummary,
    Finding,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def _evidence() -> Evidence:
    return Evidence(
        source_url="https://api.company-information.service.gov.uk/company/99999999",
        retrieved_at=NOW,
    )


def test_finding_with_empty_evidence_is_unconstructable() -> None:
    with pytest.raises(ValidationError):
        Finding(
            id="REG-001",
            stage="registry",
            severity="info",
            confidence="confirmed",
            statement="A finding with no evidence must fail validation.",
            evidence=[],
        )


def test_finding_without_evidence_field_is_unconstructable() -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate(
            {
                "id": "REG-001",
                "stage": "registry",
                "severity": "info",
                "confidence": "confirmed",
                "statement": "Still no evidence.",
            }
        )


def test_finding_assignment_cannot_remove_evidence() -> None:
    finding = Finding(
        id="REG-001",
        stage="registry",
        severity="info",
        confidence="confirmed",
        statement="Company status is active.",
        evidence=[_evidence()],
    )
    with pytest.raises(ValidationError):
        finding.evidence = []
    assert len(finding.evidence) == 1


def test_finding_with_one_evidence_item_is_valid() -> None:
    finding = Finding(
        id="REG-001",
        stage="registry",
        severity="info",
        confidence="confirmed",
        statement="Company status is active.",
        evidence=[_evidence()],
    )
    assert len(finding.evidence) == 1


def test_finding_rejects_unknown_severity() -> None:
    with pytest.raises(ValidationError):
        Finding(
            id="REG-001",
            stage="registry",
            severity="purple",  # type: ignore[arg-type]
            confidence="confirmed",
            statement="Bad severity.",
            evidence=[_evidence()],
        )


def test_company_profile_tolerates_unknown_and_deprecated_fields() -> None:
    profile = CompanyProfile.model_validate(
        {
            "company_name": "FABRICATED WIDGETS LTD",
            "company_number": "99999999",
            "type": "ltd",
            "has_charges": True,
            "has_insolvency_history": False,
            "some_future_field": {"nested": True},
        }
    )
    assert profile.company_type == "ltd"
    assert not hasattr(profile, "has_charges")


def test_accounts_overdue_reads_next_accounts() -> None:
    profile = CompanyProfile.model_validate(
        {
            "company_name": "FABRICATED WIDGETS LTD",
            "company_number": "99999999",
            "accounts": {"next_accounts": {"overdue": True}},
            "confirmation_statement": {"overdue": False},
        }
    )
    assert profile.accounts_overdue is True
    assert profile.confirmation_statement_overdue is False


def test_filing_summary_parses_reserved_names_via_aliases() -> None:
    filing = FilingSummary.model_validate(
        {"transaction_id": "fictTxn001", "type": "AA", "date": "2025-02-20"}
    )
    assert filing.filing_type == "AA"
    assert filing.date is not None and filing.date.isoformat() == "2025-02-20"


def test_casefile_defaults_keep_model_stage_empty() -> None:
    profile = CompanyProfile(company_name="FABRICATED WIDGETS LTD", company_number="99999999")
    casefile = CaseFile(
        subject=profile,
        tool_version="0.1.0.dev0",
        screened_at=NOW,
    )
    assert casefile.verdict is None
    assert casefile.claims == []
    assert casefile.assessments == []
    assert casefile.disclaimer == RESEARCH_AID_DISCLAIMER
    assert casefile.clock_override is False


def test_casefile_json_roundtrip() -> None:
    profile = CompanyProfile(company_name="FABRICATED WIDGETS LTD", company_number="99999999")
    original = CaseFile(
        subject=profile,
        findings=[
            Finding(
                id="REG-001",
                stage="registry",
                severity="info",
                confidence="confirmed",
                statement="Company status is active.",
                evidence=[_evidence()],
            )
        ],
        tool_version="0.1.0.dev0",
        screened_at=NOW,
    )
    restored = CaseFile.model_validate_json(original.model_dump_json())
    assert restored == original
