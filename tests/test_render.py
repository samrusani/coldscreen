"""Memo rendering: structure, disclaimer, attribution, verdict text."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from firstpass.models import (
    OGL_ATTRIBUTION,
    RESEARCH_AID_DISCLAIMER,
    CaseFile,
    CompanyProfile,
    DisqualificationCheck,
    Evidence,
    Finding,
    MediaItem,
    MediaScreening,
    NetworkExpansion,
    SynthesisMetadata,
    Verdict,
)
from firstpass.render import NO_SYNTHESIS_TEXT, render_memo

from .conftest import SCREENED_AT


def minimal_casefile() -> CaseFile:
    profile = CompanyProfile.model_validate(
        {
            "company_name": "FABRICATED WIDGETS LTD",
            "company_number": "99999999",
            "jurisdiction": "england-wales",
        }
    )
    finding = Finding(
        id="REG-001",
        stage="registry",
        severity="info",
        confidence="confirmed",
        statement="Company status is active.",
        evidence=[
            Evidence(
                source_url="https://api.company-information.service.gov.uk/company/99999999",
                retrieved_at=SCREENED_AT,
            )
        ],
    )
    return CaseFile(
        subject=profile,
        findings=[finding],
        tool_version="0.1.0.dev0",
        screened_at=SCREENED_AT,
    )


def test_memo_contains_header_disclaimer_and_attribution() -> None:
    memo = render_memo(minimal_casefile())
    assert memo.startswith("# Screening memo: FABRICATED WIDGETS LTD")
    assert "99999999" in memo
    assert "england-wales" in memo
    assert "2026-08-18 12:00 UTC" in memo
    assert "Tool version 0.1.0.dev0" in memo
    assert RESEARCH_AID_DISCLAIMER in memo
    assert OGL_ATTRIBUTION in memo
    assert "Open Government Licence v3.0" in memo


def test_memo_states_no_synthesis_when_no_verdict() -> None:
    memo = render_memo(minimal_casefile())
    assert NO_SYNTHESIS_TEXT in memo
    assert "No synthesis: no model configured" in memo


def test_memo_renders_the_verdict_block_with_rubric_lines() -> None:
    casefile = minimal_casefile().model_copy(
        update={
            "verdict": Verdict(
                level="amber",
                triggered=["A1", "A2"],
                rationale="Two amber triggers are met.",
                questions=["Q one?", "Q two?", "Q three?"],
            ),
            "narrative": "A short narrative.",
            "synthesis": SynthesisMetadata(provider="fake", model="canned", prompt_version="1"),
        }
    )
    memo = render_memo(casefile)
    assert "**AMBER**" in memo
    assert "- **A1** (AMBER): Overdue or irregular filings" in memo
    assert "- **A2** (AMBER): Wholesale officer changes within 12 months" in memo
    assert "Two amber triggers are met." in memo
    assert "## Narrative" in memo
    assert "A short narrative." in memo
    assert "1. Q one?" in memo
    assert "3. Q three?" in memo
    assert "provider fake, model canned, prompt version 1" in memo
    # The disclaimer sits adjacent to the verdict AND in the footer.
    assert memo.count(RESEARCH_AID_DISCLAIMER) == 2


def test_memo_renders_the_enforcement_note() -> None:
    casefile = minimal_casefile().model_copy(
        update={
            "verdict": Verdict(
                level="red",
                triggered=["R1"],
                rationale="A red trigger is met.",
                questions=["Q?", "Q?", "Q?"],
            ),
            "verdict_enforcement": "Verdict level enforced: the model proposed green.",
        }
    )
    memo = render_memo(casefile)
    assert "Note: Verdict level enforced" in memo


def test_memo_states_synthesis_failure_when_told() -> None:
    memo = render_memo(minimal_casefile(), synthesis_failure="the model never answered")
    assert "Synthesis was attempted and failed: the model never answered" in memo
    assert NO_SYNTHESIS_TEXT not in memo


def test_media_section_lists_sources_but_never_titles() -> None:
    item = MediaItem(
        title="Totally fictional headline about misconduct",
        url="https://fictional-gazette.example/a",
        source_domain="fictional-gazette.example",
        published="2026-05-14",
        query_category="misconduct",
        snippet="A snippet that stays out of the memo.",
    )
    casefile = minimal_casefile().model_copy(
        update={
            "media": MediaScreening(
                performed=True, provider="tavily", results_per_query=5, items=[item]
            )
        }
    )
    memo = render_memo(casefile)
    assert "fictional-gazette.example" in memo
    assert "https://fictional-gazette.example/a" in memo
    assert "misconduct" in memo
    assert "Totally fictional headline" not in memo
    assert "A snippet that stays out of the memo." not in memo


def test_disqualification_lines_cover_every_outcome() -> None:
    checks = [
        DisqualificationCheck(
            subject="GRINDSTONE, Gertrude", role="officer and psc", outcome="none"
        ),
        DisqualificationCheck(
            subject="OPAQUE HOLDCO LTD",
            role="officer",
            outcome="mismatch",
            detail="registered company number differs",
        ),
    ]
    casefile = minimal_casefile().model_copy(
        update={"network": NetworkExpansion(performed=True, disqualification_checks=checks)}
    )
    memo = render_memo(casefile)
    assert "GRINDSTONE, Gertrude (officer and psc): no record matched." in memo
    assert (
        "OPAQUE HOLDCO LTD (officer): name matched but identifying details differ;"
        " likely a different person or company." in memo
    )


def test_findings_are_grouped_by_severity() -> None:
    casefile = minimal_casefile()
    evidence = casefile.findings[0].evidence
    casefile.findings.append(
        Finding(
            id="REG-003",
            stage="registry",
            severity="amber",
            confidence="confirmed",
            statement="The registry marks the next accounts as overdue.",
            evidence=evidence,
        )
    )
    memo = render_memo(casefile)
    red_section = memo.split("### Red")[1].split("### Amber")[0]
    amber_section = memo.split("### Amber")[1].split("### Info")[0]
    info_section = memo.split("### Info")[1].split("## Officers")[0]
    assert "None recorded." in red_section
    assert "REG-003" in amber_section
    assert "REG-001" in info_section


def test_every_finding_line_carries_its_evidence_url() -> None:
    memo = render_memo(minimal_casefile())
    assert (
        "Evidence: https://api.company-information.service.gov.uk/company/99999999"
        " (retrieved 2026-08-18)" in memo
    )


def test_empty_sections_render_explicit_absence_lines() -> None:
    memo = render_memo(minimal_casefile())
    assert "No resignations within the lookback window." in memo
    assert "No PSC entries in this casefile." in memo
    assert "No charge entries in this casefile." in memo
    assert "No filing history entries were retrieved." in memo


def test_aware_screened_at_is_rendered_as_utc() -> None:
    casefile = minimal_casefile()
    plus_two = timezone(timedelta(hours=2))
    casefile = casefile.model_copy(
        update={"screened_at": datetime(2026, 8, 18, 14, 0, 0, tzinfo=plus_two)}
    )
    memo = render_memo(casefile)
    assert "2026-08-18 12:00 UTC" in memo
    assert "14:00" not in memo


def test_naive_screened_at_is_treated_as_utc() -> None:
    casefile = minimal_casefile()
    casefile = casefile.model_copy(update={"screened_at": datetime(2026, 8, 18, 12, 0, 0)})
    memo = render_memo(casefile)
    assert "2026-08-18 12:00 UTC" in memo


def test_clock_override_note_appears_only_when_set() -> None:
    plain = render_memo(minimal_casefile())
    assert "FIRSTPASS_SCREENED_AT" not in plain
    overridden = render_memo(minimal_casefile().model_copy(update={"clock_override": True}))
    assert "overridden through FIRSTPASS_SCREENED_AT" in overridden
