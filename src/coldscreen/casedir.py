"""Case directory writer.

The case directory is the audit pack: memo.md, casefile.json, and every raw
API response under evidence/ with an index.json manifest. Params are
sanitized before persistence as a belt and braces measure; the API key
travels only in the Authorization header and never reaches params, but any
key that looks credential-shaped is stripped anyway.

Every write goes through `write_case_text`, which refuses to follow a
symbolic link at the name it is writing. Confining the case directory is not
enough on its own: the tool-owned names inside it (memo.md, casefile.json,
evidence/*.json) can each be replaced by a link pointing anywhere the
process can write, and an ordinary write follows it.
"""

from __future__ import annotations

import errno
import json
import os
import re
from pathlib import Path
from typing import Any

from .models import CaseFile
from .stages.registry import NamedRecord

_CREDENTIAL_PARAM_RE = re.compile(r"(key|token|secret|auth|password)", re.IGNORECASE)
_COMPANY_NUMBER_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

# 0 on platforms without O_NOFOLLOW (Windows); the lstat check below covers
# those, with the race that implies.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# What a kernel returns for O_NOFOLLOW against a symlink: ELOOP on Linux and
# macOS, EMLINK on some BSDs.
_SYMLINK_ERRNOS = {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}


class UnsafeCasePath(Exception):
    """A tool-owned path inside a case directory is not safe to write.

    Raised instead of writing through a symbolic link. It is not an OSError
    subclass on purpose: callers report it as a refusal with its own message
    rather than folding it into "the disk write failed".
    """


def _symlink_refusal(path: Path, kind: str) -> UnsafeCasePath:
    return UnsafeCasePath(
        f"{path} is a symbolic link. coldscreen will not write through a link"
        f" at a {kind} it owns, because the write would land outside the case"
        " directory. Remove or rename it and run again."
    )


def refuse_symlink(path: Path, kind: str) -> None:
    """Refuse a tool-owned path that has been replaced by a symbolic link.

    Used for the directories, and ahead of a group of writes so that the
    first file is not written before a later name is found to be a link.
    `write_case_text` still refuses atomically at open time.
    """
    if path.is_symlink():
        raise _symlink_refusal(path, kind)


def write_case_text(path: Path, text: str) -> None:
    """Write one tool-owned file, refusing a symbolic link at the final name.

    O_NOFOLLOW puts the refusal in the kernel at open time, so the check and
    the write cannot come apart: there is no window in which the name is
    validated and then replaced. The mode is 0o666 before umask, which is
    what an ordinary text write would have produced.
    """
    if not _NOFOLLOW and path.is_symlink():
        raise _symlink_refusal(path, "file")
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o666)
    except OSError as error:
        if error.errno in _SYMLINK_ERRNOS:
            raise _symlink_refusal(path, "file") from None
        raise
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)


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
    """Write memo.md, casefile.json, and evidence/ under case_dir.

    Refuses any tool-owned name that is a symbolic link, directory or file.
    Callers confine `case_dir` itself; this keeps the writes inside it.
    """
    refuse_symlink(case_dir, "case directory")
    evidence_dir = case_dir / "evidence"
    refuse_symlink(evidence_dir, "evidence directory")
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
        write_case_text(
            evidence_dir / filename, json.dumps(payload, indent=2, sort_keys=True) + "\n"
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

    write_case_text(evidence_dir / "index.json", json.dumps(index, indent=2) + "\n")
    write_case_text(case_dir / "casefile.json", casefile.model_dump_json(indent=2) + "\n")
    write_case_text(case_dir / "memo.md", memo)
    return case_dir


def load_casefile(case_dir: Path) -> CaseFile:
    """Load casefile.json from a case directory, for offline rerun."""
    casefile_path = case_dir / "casefile.json"
    if not casefile_path.is_file():
        raise FileNotFoundError(f"no casefile.json in {case_dir}")
    return CaseFile.model_validate_json(casefile_path.read_text(encoding="utf-8"))
