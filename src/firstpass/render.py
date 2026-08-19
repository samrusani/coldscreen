"""Memo rendering with Jinja2.

The memo is grouped by severity, carries the research-aid disclaimer (in the
footer AND adjacent to the verdict), the Open Government Licence
attribution, and the verdict block when synthesis ran. Rendered memos never
contain media headlines or snippets: the media section lists source domain,
published date, query category, and URL only, so the mechanical language
gate holds even when coverage is about exactly the things the gate bans.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from .models import OGL_ATTRIBUTION, CaseFile, Finding
from .rubric import TRIGGER_INDEX

NO_SYNTHESIS_TEXT = (
    "No synthesis: no model configured. The findings in this memo are"
    " deterministic results from the registry, network, sanctions, and media"
    " stages; no verdict is assessed."
)


def _as_utc(value: datetime) -> datetime:
    """Aware datetimes are converted to UTC; naive ones are assumed UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fmt_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return _as_utc(value).date().isoformat()
    return value.isoformat()


def _fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("firstpass", "templates"),
        autoescape=select_autoescape(default_for_string=False, default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["fmt_date"] = _fmt_date
    env.filters["fmt_datetime"] = _fmt_datetime
    return env


_DISQ_OUTCOME_TEXT = {
    "strong_active": (
        "ACTIVE disqualification record matched on identifying details. See the red findings."
    ),
    "strong_expired": "expired disqualification record matched on identifying details.",
    "name_only": "possible record, name match only. Requires manual check.",
    "mismatch": (
        "name matched but identifying details differ; likely a different person or company."
    ),
    "none": "no record matched.",
}


def _disq_lines(casefile: CaseFile) -> list[str]:
    if casefile.network is None:
        return []
    lines = []
    for check in casefile.network.disqualification_checks:
        text = _DISQ_OUTCOME_TEXT.get(check.outcome, check.outcome)
        if check.outcome == "strong_expired" and check.detail:
            text = text.rstrip(".") + f" ({check.detail})."
        lines.append(f"{check.subject} ({check.role}): {text}")
    return lines


def _claim_rows(casefile: CaseFile) -> list[dict[str, str]]:
    """Claims-vs-evidence table rows, one per stored claim, in claim order.

    The claim text is rendered VERBATIM (quoted data): the language gates
    exempt exactly the stored claim strings, so any rewriting here would
    break the span match and turn a legitimate quotation into a gate hit.
    Puffery rows carry "not checkable"; a checkable claim with no stored
    assessment (synthesis never ran, or failed after extraction) renders
    honestly as "not assessed".
    """
    assessments = {a.claim_id: a for a in casefile.assessments}
    rows: list[dict[str, str]] = []
    for index, claim in enumerate(casefile.claims, start=1):
        if not claim.checkable:
            record, status = "", "not checkable"
        else:
            assessment = assessments.get(claim.id)
            if assessment is None:
                record, status = "", "not assessed"
            else:
                record, status = assessment.record_note, assessment.status.capitalize()
        rows.append(
            {
                "number": str(index),
                "text": claim.text,
                "source": claim.source,
                "record": record,
                "status": status,
            }
        )
    return rows


def _trigger_lines(triggered: list[str]) -> list[dict[str, str]]:
    """Rubric line per cited trigger; unknown ids are labelled as such."""
    lines: list[dict[str, str]] = []
    for trigger_id in triggered:
        trigger = TRIGGER_INDEX.get(trigger_id)
        if trigger is None:
            lines.append({"id": trigger_id, "severity": "?", "text": "not in the rubric catalog"})
        else:
            lines.append(
                {"id": trigger.id, "severity": trigger.severity.upper(), "text": trigger.text}
            )
    return lines


def render_memo(casefile: CaseFile, synthesis_failure: str | None = None) -> str:
    """Render memo.md content from a CaseFile.

    synthesis_failure is set by the CLI when synthesis was attempted and
    failed cleanly this run: the memo then states the failure instead of
    implying that no model was configured. It is run state, not casefile
    state, so it is never persisted.
    """
    by_severity: dict[str, list[Finding]] = {"red": [], "amber": [], "info": []}
    for finding in casefile.findings:
        by_severity[finding.severity].append(finding)

    current_officers = [o for o in casefile.officers if o.resigned_on is None]
    resigned_officers = [o for o in casefile.officers if o.resigned_on is not None]

    context: dict[str, Any] = {
        "c": casefile,
        "findings_red": by_severity["red"],
        "findings_amber": by_severity["amber"],
        "findings_info": by_severity["info"],
        "current_officers": current_officers,
        "resigned_officers": resigned_officers,
        "no_synthesis_text": NO_SYNTHESIS_TEXT,
        "synthesis_failure": synthesis_failure,
        "claim_rows": _claim_rows(casefile),
        "trigger_lines": _trigger_lines(casefile.verdict.triggered) if casefile.verdict else [],
        "disq_lines": _disq_lines(casefile),
        "ogl_attribution": OGL_ATTRIBUTION,
    }
    template = _environment().get_template("memo.md.j2")
    return template.render(**context)
