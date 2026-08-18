<!-- Draft launch README. Working name "firstpass" is a placeholder. Complete the verify-before-build checklist in ARCHITECTURE.md section 16 before publishing. -->

# firstpass

First-pass deal screening from public sources. One command in, one screening memo out.

`firstpass` takes a company name, pulls the public record, and produces the memo a corporate finance analyst would write before anyone agrees to a meeting: registry profile, officer and ownership network, sanctions and PEP exposure, adverse media, and a claims-vs-evidence table showing what the company says about itself against what the public record actually supports.

It is not due diligence. It is the screen that decides whether due diligence is worth anyone's time.

## Where this comes from

[Type3 Capital](https://type3.capital) is a licensed corporate finance advisory firm. Every inbound mandate goes through the same outside-in screen before we engage: no data room, no management calls, public sources only. In recent months that screen led us to decline two inbound mandates after central marketing claims failed to survive contact with the public record.

This repository is that screen, generalized and automated. A methodology like this gets stronger in public, not weaker, so we open-sourced it.

## What it checks

**Registry pass.** Company profile, incorporation history, officers (current and recently resigned), persons with significant control, filing history, registered charges, insolvency events.

**Network expansion.** Each officer's other active appointments and any disqualifications. Undisclosed related-party webs show up here.

**Sanctions and PEP.** Entity, officers, and PSCs matched against OpenSanctions data.

**Adverse media.** Structured search across fraud, insolvency, regulatory, and litigation queries, with sources and dates recorded.

**Claims vs evidence.** Discrete, checkable claims extracted from the pitch deck and website, then tested against the record. This table is the point of the whole exercise.

## Quickstart

```bash
pip install firstpass-screen
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY, or point at local Ollama
export COMPANIES_HOUSE_API_KEY=...  # free key from the Companies House developer hub

firstpass screen "Acme Holdings Ltd"
```

With a deck and a website:

```bash
firstpass screen 01234567 --deck pitch.pdf --site https://example.com
```

Output lands in `cases/acme-holdings-01234567/`: the memo, plus every piece of raw evidence as JSON with source URLs and retrieval timestamps.

## Example output (abridged, fictional company)

```
# Screening memo: Acme Holdings Ltd
Company 01234567 (England and Wales) · Screened 2026-08-18 · firstpass v0.1

## Verdict: AMBER
Proceed only after the three clarifications below. Two deck claims are
contradicted by the public record, and the charge stack is heavier than
the equity story implies.

## Claims vs evidence
| # | Claim (deck p.4)       | Public record                        | Status       |
|---|------------------------|--------------------------------------|--------------|
| 1 | "Operating since 2015" | Incorporated 2021-03-11              | Contradicted |
| 2 | "Debt free"            | Two outstanding charges (2023, 2024) | Contradicted |
| 3 | "Team of 40"           | No public headcount source found     | Unverified   |

## Clarification questions to send
1. Reconcile "operating since 2015" with a 2021 incorporation date...
```

## How verdicts work

Verdicts follow a published rubric (`rubric.md`), version-controlled with the code so the logic is auditable.

**RED**: sanctions or PEP match on the entity or its PSCs; active insolvency; a disqualified director in the current officer set; a material claim directly contradicted by the registry; an undisclosed related-party network around the officers.

**AMBER**: overdue or irregular filings; recent wholesale officer changes; registered charges inconsistent with the stated capital story; central claims that no public source can verify; corporate age or scale inconsistent with the stated history.

**GREEN**: clean registry, claims supported or plausibly verifiable, no substantive adverse media.

A verdict is an opinion generated from public sources at a point in time. It is a reason to ask better questions, not a substitute for judgment.

## Evidence discipline

- Every finding carries a source URL and a retrieval date. A finding without evidence fails validation and is dropped.
- Absence is recorded. "No insolvency notices found" is a finding, not silence.
- Every finding carries a confidence tag: confirmed, indicated, or unverified.
- Facts are fetched by code. The model synthesizes and drafts, but it never supplies a registry fact from its own memory.
- Public sources only. Nothing behind a login, no scraping in breach of a site's terms.

## Models

Works with hosted models (Anthropic, OpenAI) or local models via Ollama. The deterministic collection layer is identical regardless of model; only synthesis quality varies. Run it fully local if your deal flow should never touch a third-party API.

## Scope and non-goals (v0.1)

- UK companies only. Sweden (Bolagsverket) and US (SEC EDGAR) adapters are next; the adapter interface is documented in `ARCHITECTURE.md`.
- No financial statement analysis. Filings are inventoried, not parsed.
- No people search beyond registered officers and PSCs.
- No continuous monitoring. One screen, one memo.
- No data room ingestion. Other open projects cover post-engagement document review; this tool runs before any documents change hands.

## Data sources

- **Companies House** (UK registry): free API, key required. You are bound by their reuse terms.
- **OpenSanctions**: sanctions, PEP, and criminal watchlist data. Check their current data license before commercial use; the code license of this repo does not extend to their data.
- **Web search**: via your model provider's search tool or a pluggable search API.

## Disclaimer

`firstpass` is a research aid. It is not investment advice, not a credit reference, and not a consumer report. Do not use it for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. Officer data is personal data sourced from public registers; you are responsible for processing it lawfully in your jurisdiction. Verify everything independently before acting on it. Memos describe what the public record shows and with what confidence; they do not make accusations.

## Roadmap

- Sweden and US registry adapters
- JSON output mode for pipelines
- MCP server mode, so the screen runs inside agent workflows
- Better deck parsing (tables, charts)

## Contributing

Issues and PRs welcome. The highest-value contribution is a registry adapter for your jurisdiction; the interface is small and documented in `ARCHITECTURE.md`.

## License

MIT. Data sources retain their own licenses.

## Maintainer

Maintained by the team behind [Type3 Capital](https://type3.capital). Maintainer GitHub handle lands here at launch.
