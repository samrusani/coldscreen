"""Memo rendering with Jinja2.

The memo is grouped by severity, carries the research-aid disclaimer and the
Open Government Licence attribution, and states explicitly that no verdict
is assessed in the deterministic milestone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from .models import OGL_ATTRIBUTION, CaseFile, Finding

VERDICT_NOT_ASSESSED = (
    "Not assessed. Deterministic registry pass only; synthesis is not part of this milestone."
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


def render_memo(casefile: CaseFile) -> str:
    """Render memo.md content from a CaseFile."""
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
        "verdict_text": VERDICT_NOT_ASSESSED,
        "ogl_attribution": OGL_ATTRIBUTION,
    }
    template = _environment().get_template("memo.md.j2")
    return template.render(**context)
