"""Case directory writer.

The case directory is the audit pack: memo.md, casefile.json, and every raw
API response under evidence/ with an index.json manifest. Params are
sanitized before persistence as a belt and braces measure; the API key
travels only in the Authorization header and never reaches params, but any
key that looks credential-shaped is stripped anyway.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import CaseFile
from .stages.registry import NamedRecord

_CREDENTIAL_PARAM_RE = re.compile(r"(key|token|secret|auth|password)", re.IGNORECASE)
_COMPANY_NUMBER_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


def slugify(name: str, max_length: int = 60) -> str:
    """Lowercase, non-alphanumerics to single hyphens, trimmed."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "company"


def validate_company_number(company_number: str) -> str:
    """Reject anything that is not plain alphanumeric before it becomes a
    URL path segment or a directory name."""
    if not _COMPANY_NUMBER_RE.match(company_number):
        raise ValueError(f"not a valid company number: {company_number!r}")
    return company_number


def case_dir_name(company_name: str, company_number: str) -> str:
    validate_company_number(company_number)
    return f"{slugify(company_name)}-{company_number.lower()}"


def sanitize_params(params: dict[str, str]) -> dict[str, str]:
    """Drop any param whose name looks credential-shaped before persisting."""
    return {k: v for k, v in params.items() if not _CREDENTIAL_PARAM_RE.search(k)}


def write_case(
    case_dir: Path,
    casefile: CaseFile,
    records: list[NamedRecord],
    memo: str,
) -> Path:
    """Write memo.md, casefile.json, and evidence/ under case_dir."""
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    index: list[dict[str, Any]] = []
    for named in records:
        record = named.record
        filename = f"{named.name}.json"
        payload = {
            "name": named.name,
            "url": record.url,
            "params": sanitize_params(record.params),
            "status": record.status,
            "retrieved_at": record.retrieved_at.isoformat(),
            "from_cache": record.from_cache,
            "body": record.body,
        }
        (evidence_dir / filename).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        index.append(
            {
                "name": named.name,
                "file": f"evidence/{filename}",
                "url": record.url,
                "status": record.status,
                "retrieved_at": record.retrieved_at.isoformat(),
            }
        )

    (evidence_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        casefile.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (case_dir / "memo.md").write_text(memo, encoding="utf-8")
    return case_dir


def load_casefile(case_dir: Path) -> CaseFile:
    """Load casefile.json from a case directory, for offline rerun."""
    casefile_path = case_dir / "casefile.json"
    if not casefile_path.is_file():
        raise FileNotFoundError(f"no casefile.json in {case_dir}")
    return CaseFile.model_validate_json(casefile_path.read_text(encoding="utf-8"))
