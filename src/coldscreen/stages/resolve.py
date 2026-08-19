"""Stage 1: entity resolution.

Input that looks like a company number is used directly. Anything else goes
through registry search. Ambiguity is surfaced to the caller, never resolved
silently: the CLI turns it into an interactive picker on a terminal and an
exit with the candidate table otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..ch_client import CompaniesHouseClient, FetchRecord
from ..models import CompanyCandidate

# 6 to 8 digits (zero-padded to 8), or a two-letter prefix plus 6 digits.
_NUMBER_RE = re.compile(r"^(?:\d{6,8}|[A-Z]{2}\d{6})$")


def normalize_company_number(text: str) -> str | None:
    """Return the normalized company number when the input looks like one."""
    candidate = re.sub(r"\s+", "", text).upper()
    if not _NUMBER_RE.match(candidate):
        return None
    if candidate.isdigit():
        return candidate.zfill(8)
    return candidate


@dataclass(frozen=True)
class Resolution:
    """Outcome of stage 1.

    exact_title_match is True when the number was chosen because exactly one
    candidate's title matched the query; set_aside counts the other
    candidates that were passed over. Callers must surface that to the user:
    a resolved ambiguity is still a choice someone should be able to see.
    """

    company_number: str | None
    candidates: list[CompanyCandidate] = field(default_factory=list)
    search_record: FetchRecord | None = None
    chosen: CompanyCandidate | None = None
    exact_title_match: bool = False
    set_aside: int = 0

    @property
    def is_ambiguous(self) -> bool:
        return self.company_number is None and len(self.candidates) > 1

    @property
    def is_empty(self) -> bool:
        return self.company_number is None and not self.candidates


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def resolve(client: CompaniesHouseClient, query: str, items_per_page: int = 20) -> Resolution:
    """Resolve free text to a company number, or surface the candidates.

    Rules, smallest first: input shaped like a company number is taken as
    one. A search returning exactly one candidate proceeds. A search where
    exactly one candidate's title matches the query (ignoring case and
    punctuation) proceeds with that candidate. Everything else is ambiguous.
    """
    number = normalize_company_number(query)
    if number is not None:
        return Resolution(company_number=number)

    record = client.search_companies(query, items_per_page=items_per_page)
    body = record.body if isinstance(record.body, dict) else {}
    raw_items = body.get("items") or []
    candidates = [
        CompanyCandidate.model_validate(item) for item in raw_items if isinstance(item, dict)
    ]
    if len(candidates) == 1:
        return Resolution(
            company_number=candidates[0].company_number,
            candidates=candidates,
            search_record=record,
            chosen=candidates[0],
        )
    wanted = _normalize_title(query)
    exact = [c for c in candidates if _normalize_title(c.title) == wanted]
    if len(exact) == 1:
        return Resolution(
            company_number=exact[0].company_number,
            candidates=candidates,
            search_record=record,
            chosen=exact[0],
            exact_title_match=True,
            set_aside=len(candidates) - 1,
        )
    return Resolution(company_number=None, candidates=candidates, search_record=record)
