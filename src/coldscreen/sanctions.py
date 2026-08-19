"""Stage 4: sanctions and PEP screening against OpenSanctions (or yente).

Coded from wiki/research/opensanctions.md and
wiki/research/ftm-and-disqualifications.md, not from memory:

- POST {base}/match/{dataset}, header "Authorization: ApiKey <key>". The
  header is OMITTED entirely when no key is configured (self-hosted yente).
  The key is never logged, never persisted, never placed in URLs or params.
- Body {"queries": {qid: {"schema": ..., "properties": {...}}}}, all
  property values lists of strings, up to 100 queries per request.
- Params: threshold (default 0.7), algorithm (default "best"), limit
  (default 5). "best" drifts over time, so any resolved algorithm name the
  response exposes is recorded next to the requested one.
- People are queried as schema Person, the subject as schema Company:
  properties not belonging to the queried schema are silently dropped
  upstream, so a birthDate on a Company query would vanish without error.
- Partial birth dates are officially accepted ("1975", "1975-06").

No key and no custom endpoint means the stage is SKIPPED with an explicit
finding: absence of screening is data and must be visible in the memo.
The tool never bundles a key; every user brings their own licence and key.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_random_exponential

from .ch_client import FetchRecord
from .config import DEFAULT_OPENSANCTIONS_BASE_URL, Settings
from .models import (
    PSC,
    CompanyProfile,
    Evidence,
    Finding,
    Officer,
    SanctionsScreening,
    SanctionsSubjectResult,
)
from .stages.network import HONORIFICS, duplicate_officer_index
from .stages.registry import NamedRecord

STAGE = "sanctions"
MAX_QUERIES_PER_REQUEST = 100
MAX_ATTEMPTS = 5
NOT_RUN_URL = "coldscreen:not-run/sanctions"
FAILED_URL = "coldscreen:failed/sanctions"


class SanctionsError(Exception):
    """Sanctions screening failed after retries. Message is user-facing."""


class _RetryableSanctionsError(SanctionsError):
    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"retryable status {status} from {url}")
        self.status = status


class _SanctionsTransportError(SanctionsError):
    def __init__(self, url: str, cause: Exception) -> None:
        super().__init__(f"could not reach the sanctions endpoint at {url}: {cause}")


@dataclass(frozen=True)
class SanctionsSubject:
    """One query subject with its FollowTheMoney query body.

    kind "officer and psc" marks one person holding both roles, screened
    with a single query.
    """

    name: str
    kind: Literal["company", "officer", "psc", "officer and psc"]
    query_schema: Literal["Person", "Company"]
    properties: dict[str, list[str]]


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _person_names(raw_name: str) -> dict[str, list[str]]:
    """Name properties for a Person query.

    Registry officer names arrive as "SURNAME, Forename(s)": disaggregated
    firstName/lastName beats a single name per the matching guidance, and
    the natural-order full name goes in name. PSC names arrive in natural
    order, possibly with an honorific, so those only fill name.
    """
    name = _clean(raw_name)
    if "," in name:
        surname, _, forenames = name.partition(",")
        surname = surname.strip()
        forenames = forenames.strip()
        properties: dict[str, list[str]] = {}
        natural = f"{forenames} {surname}".strip()
        if natural:
            properties["name"] = [natural]
        if forenames:
            properties["firstName"] = [forenames]
        if surname:
            properties["lastName"] = [surname]
        return properties
    tokens = name.split()
    while tokens and tokens[0].lower().rstrip(".") in HONORIFICS:
        tokens = tokens[1:]
    cleaned = " ".join(tokens) or name
    return {"name": [cleaned]}


def _birth_date(dob: Any) -> str | None:
    """Partial FtM birthDate from the registry's {month, year} object."""
    if dob is None:
        return None
    year = getattr(dob, "year", None)
    month = getattr(dob, "month", None)
    if not isinstance(year, int):
        return None
    if isinstance(month, int) and 1 <= month <= 12:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


def build_subjects(
    profile: CompanyProfile,
    current_officers: list[Officer],
    pscs: list[PSC],
) -> list[SanctionsSubject]:
    """The company, every current officer, every individual PSC, in order.

    One human, one query: an individual PSC who is also a current officer
    (same normalized name AND same DOB month and year) is folded into the
    officer's query and the merged subject carries kind "officer and psc".
    """
    subjects: list[SanctionsSubject] = []

    company_properties: dict[str, list[str]] = {"name": [profile.company_name]}
    # UK registry, so the FtM jurisdiction is the country code, not the
    # registry's internal england-wales style value.
    company_properties["jurisdiction"] = ["gb"]
    if _clean(profile.company_number):
        company_properties["registrationNumber"] = [profile.company_number]
    subjects.append(
        SanctionsSubject(
            name=profile.company_name,
            kind="company",
            query_schema="Company",
            properties=company_properties,
        )
    )

    remaining_pscs: list[PSC] = []
    merged_officer_indices: set[int] = set()
    for psc in pscs:
        if not psc.is_individual or not psc.name:
            continue
        index = duplicate_officer_index(psc.name, psc.date_of_birth, current_officers)
        if index is None:
            remaining_pscs.append(psc)
        else:
            merged_officer_indices.add(index)

    for index, officer in enumerate(current_officers):
        properties = _person_names(officer.name)
        birth = _birth_date(officer.date_of_birth)
        if birth:
            properties["birthDate"] = [birth]
        nationality = _clean(officer.nationality)
        if nationality:
            properties["nationality"] = [nationality.lower()]
        kind: Literal["officer", "officer and psc"] = (
            "officer and psc" if index in merged_officer_indices else "officer"
        )
        subjects.append(
            SanctionsSubject(
                name=officer.name,
                kind=kind,
                query_schema="Person",
                properties=properties,
            )
        )

    for psc in remaining_pscs:
        properties = _person_names(psc.name or "")
        birth = _birth_date(psc.date_of_birth)
        if birth:
            properties["birthDate"] = [birth]
        nationality = _clean(psc.nationality)
        if nationality:
            properties["nationality"] = [nationality.lower()]
        subjects.append(
            SanctionsSubject(
                name=psc.name or "",
                kind="psc",
                query_schema="Person",
                properties=properties,
            )
        )

    return subjects


def query_id(index: int) -> str:
    return f"q{index + 1:03d}"


def build_queries(subjects: list[SanctionsSubject]) -> dict[str, dict[str, Any]]:
    return {
        query_id(index): {"schema": subject.query_schema, "properties": subject.properties}
        for index, subject in enumerate(subjects)
    }


class OpenSanctionsClient:
    """Minimal client for POST /match/{dataset}, hosted or self-hosted yente.

    One configurable base URL covers both deployments; the auth header is
    present exactly when a key is configured. Retries on 429, 5xx, and
    transport failures with jittered exponential backoff.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OPENSANCTIONS_BASE_URL,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleeper if sleeper is not None else time.sleep
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"ApiKey {api_key}"
        self._http = httpx.Client(base_url=self._base_url, timeout=timeout_seconds, headers=headers)

    def __repr__(self) -> str:  # never expose the key
        return f"OpenSanctionsClient(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    def match(
        self,
        dataset: str,
        queries: dict[str, dict[str, Any]],
        threshold: float,
        algorithm: str,
        limit: int,
    ) -> FetchRecord:
        url = f"{self._base_url}/match/{dataset}"
        params = {
            "threshold": str(threshold),
            "algorithm": algorithm,
            "limit": str(limit),
        }
        retrying = Retrying(
            retry=retry_if_exception_type((_RetryableSanctionsError, _SanctionsTransportError)),
            wait=wait_random_exponential(multiplier=1, max=30),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            sleep=self._sleep,
            reraise=True,
        )
        response: httpx.Response | None = None
        for attempt in retrying:
            with attempt:
                try:
                    response = self._http.post(
                        f"/match/{dataset}", params=params, json={"queries": queries}
                    )
                except httpx.TransportError as error:
                    raise _SanctionsTransportError(url, error) from error
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableSanctionsError(response.status_code, url)
        assert response is not None
        retrieved_at = self._now()
        if response.status_code != 200:
            raise SanctionsError(
                f"the sanctions endpoint returned HTTP {response.status_code} from {url}."
                " Check the API key and endpoint configuration."
            )
        try:
            body = response.json()
        except ValueError:
            raise SanctionsError(
                f"the sanctions endpoint returned invalid JSON from {url}"
            ) from None
        return FetchRecord(
            url=url,
            params=params,
            status=response.status_code,
            body=body,
            retrieved_at=retrieved_at,
        )


@dataclass
class SanctionsStageResult:
    findings: list[Finding] = field(default_factory=list)
    records: list[NamedRecord] = field(default_factory=list)
    screening: SanctionsScreening = field(
        default_factory=lambda: SanctionsScreening(performed=False)
    )
    # Set when the stage was attempted and failed after retries. The CLI
    # reports it, exits 1, and still writes the case directory: failure is
    # loudly recorded data, never a silent abort.
    failed_reason: str | None = None


def _extract_resolved_algorithm(body: Any) -> str | None:
    """Best-effort read of the resolved algorithm name, parsed defensively."""
    if not isinstance(body, dict):
        return None
    for key in ("algorithm", "used_algorithm", "matcher_algorithm"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def _extract_dataset_release(body: Any) -> str | None:
    """Best-effort read of a dataset release or version marker, defensively.

    Neither the hosted API nor yente documents a stable field for this, so
    several plausible spellings are checked; whatever the response exposes
    is recorded verbatim, and absence stays None. The raw evidence file
    keeps the full response either way.
    """
    if not isinstance(body, dict):
        return None
    for key in ("release", "dataset_release", "dataset_version", "index_version"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for nested_key in ("version", "name"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested:
                    return nested
    return None


def _subject_results(body: Any, qid: str) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    responses = body.get("responses")
    if not isinstance(responses, dict):
        return []
    entry = responses.get(qid)
    if not isinstance(entry, dict):
        return []
    results = entry.get("results")
    if not isinstance(results, list):
        return []
    return [r for r in results if isinstance(r, dict)]


def run_sanctions(
    profile: CompanyProfile,
    current_officers: list[Officer],
    pscs: list[PSC],
    settings: Settings,
    api_key: str | None,
    base_url: str | None,
    now: Callable[[], datetime],
    sleeper: Callable[[float], None] | None = None,
) -> SanctionsStageResult:
    """Run stage 4, or record the explicit skip when nothing is configured."""
    result = SanctionsStageResult()

    if api_key is None and base_url is None:
        # No key and no self-hosted endpoint: screening cannot run. The skip
        # is a first-class finding backed by a synthetic note record that is
        # clearly marked as not_run, because absence of screening is data.
        note_record = FetchRecord(
            url=NOT_RUN_URL,
            params={},
            status=0,
            body={
                "kind": "not_run",
                "stage": "sanctions",
                "reason": (
                    "no OpenSanctions API key or custom endpoint configured;"
                    " set OPENSANCTIONS_API_KEY or OPENSANCTIONS_BASE_URL"
                ),
            },
            retrieved_at=now(),
        )
        result.records.append(NamedRecord("sanctions_not_run", note_record))
        result.screening = SanctionsScreening(
            performed=False,
            skipped_reason="no OpenSanctions key or endpoint configured",
        )
        result.findings.append(
            Finding(
                id="SAN-000",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    "Sanctions screening not performed: no OpenSanctions key or"
                    " endpoint configured. Subjects were not screened against any"
                    " sanctions or PEP dataset this run."
                ),
                evidence=[
                    Evidence(
                        source_url=NOT_RUN_URL,
                        retrieved_at=note_record.retrieved_at,
                        excerpt="kind=not_run",
                    )
                ],
            )
        )
        return result

    endpoint = (base_url or DEFAULT_OPENSANCTIONS_BASE_URL).rstrip("/")
    subjects = build_subjects(profile, current_officers, pscs)
    client = OpenSanctionsClient(
        base_url=endpoint,
        api_key=api_key,
        timeout_seconds=settings.timeout_seconds,
        now=now,
        sleeper=sleeper,
    )
    records: list[FetchRecord] = []
    failure: SanctionsError | None = None
    try:
        for chunk_index in range(0, len(subjects), MAX_QUERIES_PER_REQUEST):
            chunk = subjects[chunk_index : chunk_index + MAX_QUERIES_PER_REQUEST]
            queries = {
                query_id(chunk_index + offset): {
                    "schema": subject.query_schema,
                    "properties": subject.properties,
                }
                for offset, subject in enumerate(chunk)
            }
            record = client.match(
                settings.sanctions_dataset,
                queries,
                threshold=settings.sanctions_threshold,
                algorithm=settings.sanctions_algorithm,
                limit=settings.sanctions_limit,
            )
            records.append(record)
            chunk_number = chunk_index // MAX_QUERIES_PER_REQUEST + 1
            name = "sanctions_match" if chunk_number == 1 else f"sanctions_match_{chunk_number}"
            result.records.append(NamedRecord(name, record))
    except SanctionsError as error:
        # Failure posture: keep every response already gathered, record the
        # failure loudly, and let the run continue to persistence. Absence
        # is data; failure is loudly recorded data.
        failure = error
    finally:
        client.close()

    if failure is not None:
        failed_record = FetchRecord(
            url=FAILED_URL,
            params={},
            status=0,
            body={"kind": "stage_failed", "stage": "sanctions", "error": str(failure)},
            retrieved_at=now(),
        )
        result.records.append(NamedRecord("sanctions_failed", failed_record))
        result.screening = SanctionsScreening(
            performed=False,
            failed=True,
            skipped_reason="the sanctions screening stage failed after retries",
            dataset=settings.sanctions_dataset,
            threshold=settings.sanctions_threshold,
            algorithm_requested=settings.sanctions_algorithm,
            limit=settings.sanctions_limit,
            endpoint=endpoint,
        )
        result.failed_reason = str(failure)
        result.findings.append(
            Finding(
                id="SAN-999",
                stage=STAGE,
                severity="amber",
                confidence="confirmed",
                statement=(
                    "Sanctions screening was attempted and FAILED after retries:"
                    " the endpoint could not be used, so no subject received a"
                    " screening result this run. A failed stage is not a clean"
                    " result, and it is not the same as screening that was never"
                    " configured."
                ),
                evidence=[
                    Evidence(
                        source_url=FAILED_URL,
                        retrieved_at=failed_record.retrieved_at,
                        excerpt="kind=stage_failed",
                    )
                ],
            )
        )
        return result

    threshold = settings.sanctions_threshold
    resolved = next((a for a in (_extract_resolved_algorithm(r.body) for r in records) if a), None)
    release = next(
        (value for value in (_extract_dataset_release(r.body) for r in records) if value), None
    )
    result.screening = SanctionsScreening(
        performed=True,
        dataset=settings.sanctions_dataset,
        threshold=threshold,
        algorithm_requested=settings.sanctions_algorithm,
        algorithm_resolved=resolved,
        limit=settings.sanctions_limit,
        dataset_release=release,
        endpoint=endpoint,
    )

    algorithm_text = settings.sanctions_algorithm
    if resolved and resolved != settings.sanctions_algorithm:
        algorithm_text = f"{settings.sanctions_algorithm} (resolved: {resolved})"

    for index, subject in enumerate(subjects):
        record = records[index // MAX_QUERIES_PER_REQUEST]
        qid = query_id(index)
        results = _subject_results(record.body, qid)
        scores = [r.get("score") for r in results]
        numeric_scores = [s for s in scores if isinstance(s, int | float)]
        top_score = max(numeric_scores) if numeric_scores else None
        matches = [
            r
            for r in results
            if r.get("match") is True
            or (isinstance(r.get("score"), int | float) and float(r["score"]) >= threshold)
        ]
        finding_id = f"SAN-{index + 1:03d}"
        evidence = [
            Evidence(
                source_url=record.url,
                retrieved_at=record.retrieved_at,
                excerpt=(
                    f"query {qid}: dataset={settings.sanctions_dataset}"
                    f" threshold={threshold} algorithm={algorithm_text}"
                ),
            )
        ]
        if matches:
            datasets = sorted(
                {str(d) for m in matches for d in (m.get("datasets") or []) if isinstance(d, str)}
            )
            match_scores = [
                float(m["score"]) for m in matches if isinstance(m.get("score"), int | float)
            ]
            best = max(match_scores) if match_scores else top_score
            result.screening.results.append(
                SanctionsSubjectResult(
                    subject=subject.name,
                    kind=subject.kind,
                    query_schema=subject.query_schema,
                    matched=True,
                    top_score=best,
                    datasets=datasets,
                )
            )
            score_text = f"{best:.2f}" if best is not None else "unreported"
            if subject.kind == "officer":
                # Rubric 0.2 narrows R1 to entity and PSC matches. An officer
                # match is reported at amber severity with wording that says
                # so plainly; it feeds no red trigger.
                result.findings.append(
                    Finding(
                        id=finding_id,
                        stage=STAGE,
                        severity="amber",
                        confidence="confirmed",
                        statement=(
                            f"Sanctions screening match for {subject.name}"
                            f" (officer): top score {score_text} at or above"
                            f" threshold {threshold} in dataset(s)"
                            f" {', '.join(datasets) or settings.sanctions_dataset}"
                            f" (algorithm {algorithm_text}). Officer matches are"
                            " reported for review at amber severity per rubric"
                            " 0.2; they do not trigger R1, which covers the"
                            " entity and PSCs only. Identity match confidence,"
                            " not a finding of wrongdoing; verify against the"
                            " cited evidence file."
                        ),
                        evidence=evidence,
                    )
                )
            else:
                result.findings.append(
                    Finding(
                        id=finding_id,
                        stage=STAGE,
                        severity="red",
                        confidence="confirmed",
                        statement=(
                            f"Sanctions screening match for {subject.name}"
                            f" ({subject.kind}): top score {score_text} at or above"
                            f" threshold {threshold} in dataset(s)"
                            f" {', '.join(datasets) or settings.sanctions_dataset}"
                            f" (algorithm {algorithm_text}). Identity match confidence,"
                            " not a finding of wrongdoing; verify against the cited"
                            " evidence file."
                        ),
                        evidence=evidence,
                    )
                )
        elif top_score is not None:
            result.screening.results.append(
                SanctionsSubjectResult(
                    subject=subject.name,
                    kind=subject.kind,
                    query_schema=subject.query_schema,
                    matched=False,
                    top_score=top_score,
                )
            )
            result.findings.append(
                Finding(
                    id=finding_id,
                    stage=STAGE,
                    severity="info",
                    confidence="confirmed",
                    statement=(
                        f"No sanctions match at or above threshold {threshold} for"
                        f" {subject.name} ({subject.kind}); nearest candidate scored"
                        f" {top_score:.2f} (dataset {settings.sanctions_dataset},"
                        f" algorithm {algorithm_text})."
                    ),
                    evidence=evidence,
                )
            )
        else:
            result.screening.results.append(
                SanctionsSubjectResult(
                    subject=subject.name,
                    kind=subject.kind,
                    query_schema=subject.query_schema,
                    matched=False,
                    top_score=None,
                )
            )
            result.findings.append(
                Finding(
                    id=finding_id,
                    stage=STAGE,
                    severity="info",
                    confidence="confirmed",
                    statement=(
                        f"No sanctions match at or above threshold {threshold} for"
                        f" {subject.name} ({subject.kind}): no candidates returned"
                        f" (dataset {settings.sanctions_dataset}, algorithm"
                        f" {algorithm_text})."
                    ),
                    evidence=evidence,
                )
            )

    return result
