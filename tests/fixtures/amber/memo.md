# Screening memo: GILDED ANVIL HOLDINGS LTD

Company 99999901 (england-wales). Screened 2026-08-18 12:00 UTC. Tool version 0.1.0.dev0.

Registered office: 7 Invented Foundry Row, Faketown, ZZ98 8ZZ, England
SIC codes: 24540

## Verdict

**AMBER**

Triggered:

- **A1** (AMBER): Overdue or irregular filings
- **A2** (AMBER): Wholesale officer changes within 12 months

A1 applies because the registry marks the next accounts overdue (REG-003). A2 applies because officer resignations in the last twelve months reached the wholesale-change threshold (REG-009). Both are confirmed registry facts. The sub-threshold sanctions score (SAN-001) and the unconfirmed media coverage (MED-001, MED-003, MED-004) are noted as context but do not meet their trigger definitions. Two AMBER triggers with no RED trigger give an AMBER verdict.

Synthesis: provider fake, model canned-amber, prompt version 5, rubric-enforced.

This memo is a research aid generated from public sources at a point in time. It is not investment advice, not a credit reference, and not a consumer report. Do not use it for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. Officer and PSC data is personal data from public registers; you are responsible for processing it lawfully. Verify everything independently before acting on it.

## Claims vs evidence

Not performed: no deck or site provided. See the EXT findings for the basis.

## Narrative

GILDED ANVIL HOLDINGS LTD is an active company incorporated on 2021-04-01 (REG-001, REG-002). Two registry signals stand out. First, the registry marks the next accounts as overdue (REG-003, confirmed). Second, three of five registered officers resigned in the twelve months before the screening date, against two currently active (REG-009, confirmed), which meets the wholesale-change threshold. One outstanding registered charge is on file (REG-006), with no insolvency history linked (REG-007). Sanctions screening ran against the configured dataset and produced no match at or above the 0.7 threshold for any subject; the nearest candidate, for the company name, scored 0.55 (SAN-001, an identity-similarity reading, not a match). Network expansion found no co-appointment overlap (NET-002) and no disqualification records for any current officer or PSC (NET-101, NET-102). The adverse media search surfaced three items connected to the company name in the misconduct, regulatory, and litigation query categories (MED-001, MED-003, MED-004); the coverage is recent but comes from sources whose standing is not established in this casefile, so it is treated as context rather than a confirmed-source trigger.

## Clarification questions

1. When will the overdue accounts for the period ending 2025-04-30 be filed, and what caused the delay?
2. What led three officers to resign between January and June 2026, and how have their responsibilities been reallocated?
3. What obligations does the outstanding charge registered in May 2024 secure, and what is its current balance?
4. Can you provide context for the recent coverage in the misconduct, regulatory, and litigation query categories listed in the media section?

## Findings

### Red

None recorded.

### Amber

- **REG-003** (confirmed) The registry marks the next accounts as overdue.
  - Evidence: https://api.company-information.service.gov.uk/company/99999901 (retrieved 2026-08-18)
- **REG-009** (confirmed) Wholesale officer changes: 3 officer(s) resigned in the 12 months before the screening date, against 2 currently active.
  - Evidence: https://api.company-information.service.gov.uk/company/99999901/officers (retrieved 2026-08-18)

### Info

- **REG-001** (confirmed) Company status is active.
  - Evidence: https://api.company-information.service.gov.uk/company/99999901 (retrieved 2026-08-18)
- **REG-002** (confirmed) Incorporated on 2021-04-01 (5 full years before the screening date).
  - Evidence: https://api.company-information.service.gov.uk/company/99999901 (retrieved 2026-08-18)
- **REG-006** (confirmed) Charges register: 1 charge(s) listed (1 outstanding, 0 satisfied).
  - Evidence: https://api.company-information.service.gov.uk/company/99999901/charges (retrieved 2026-08-18)
- **REG-007** (confirmed) No insolvency history is registered. The company profile offers no insolvency link, so the insolvency resource was not queried.
  - Evidence: https://api.company-information.service.gov.uk/company/99999901 (retrieved 2026-08-18)
- **REG-008** (confirmed) 2 active officer(s) on the register.
  - Evidence: https://api.company-information.service.gov.uk/company/99999901/officers (retrieved 2026-08-18)
- **REG-010** (confirmed) The company has 1 previous name(s) on the register: AURUM ANVIL LTD.
  - Evidence: https://api.company-information.service.gov.uk/company/99999901 (retrieved 2026-08-18)
- **REG-011** (confirmed) 1 PSC entry(ies) on the register, 1 not ceased. Natures of control: ownership-of-shares-75-to-100-percent.
  - Evidence: https://api.company-information.service.gov.uk/company/99999901/persons-with-significant-control (retrieved 2026-08-18)
- **NET-001** (confirmed) Network expansion covered 2 current officer(s): 1 current appointment(s) at other companies were found.
  - Evidence: https://api.company-information.service.gov.uk/officers/fictGold001/appointments (retrieved 2026-08-18)
  - Evidence: https://api.company-information.service.gov.uk/officers/fictGold002/appointments (retrieved 2026-08-18)
- **NET-002** (confirmed) No co-appointment overlap: no other company was found where two or more current officers of the subject hold current appointments.
  - Evidence: https://api.company-information.service.gov.uk/officers/fictGold001/appointments (retrieved 2026-08-18)
  - Evidence: https://api.company-information.service.gov.uk/officers/fictGold002/appointments (retrieved 2026-08-18)
- **NET-101** (confirmed) No disqualification record matches ANVIL, Arabella (officer and PSC) on the disqualified officers register.
  - Evidence: https://api.company-information.service.gov.uk/search/disqualified-officers (retrieved 2026-08-18)
- **NET-102** (confirmed) No disqualification record matches FORGE, Fitzwilliam on the disqualified officers register.
  - Evidence: https://api.company-information.service.gov.uk/search/disqualified-officers (retrieved 2026-08-18)
- **SAN-001** (confirmed) No sanctions match at or above threshold 0.7 for GILDED ANVIL HOLDINGS LTD (company); nearest candidate scored 0.55 (dataset default, algorithm best).
  - Evidence: https://api.opensanctions.org/match/default (retrieved 2026-08-18)
- **SAN-002** (confirmed) No sanctions match at or above threshold 0.7 for ANVIL, Arabella (officer and psc): no candidates returned (dataset default, algorithm best).
  - Evidence: https://api.opensanctions.org/match/default (retrieved 2026-08-18)
- **SAN-003** (confirmed) No sanctions match at or above threshold 0.7 for FORGE, Fitzwilliam (officer): no candidates returned (dataset default, algorithm best).
  - Evidence: https://api.opensanctions.org/match/default (retrieved 2026-08-18)
- **MED-001** (confirmed) Adverse media query category 'misconduct': 1 result(s) returned, 1 kept after deduplication across 2 query variant(s).
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
- **MED-002** (confirmed) Adverse media query category 'insolvency': 0 result(s) returned, 0 kept after deduplication across 2 query variant(s).
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
- **MED-003** (confirmed) Adverse media query category 'regulatory': 1 result(s) returned, 1 kept after deduplication across 2 query variant(s).
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
- **MED-004** (confirmed) Adverse media query category 'litigation': 1 result(s) returned, 1 kept after deduplication across 2 query variant(s).
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
- **MED-005** (confirmed) Adverse media query category 'sanctions': 0 result(s) returned, 0 kept after deduplication across 2 query variant(s).
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
  - Evidence: https://api.tavily.com/search (retrieved 2026-08-18)
- **EXT-000** (confirmed) Claims extraction not performed: no deck or site was provided. What the company says about itself was not screened this run.
  - Evidence: coldscreen:not-run/claims (retrieved 2026-08-18)

## Officers

### Current

| Name | Role | Appointed |
|---|---|---|
| ANVIL, Arabella | director | 2021-04-01 |
| FORGE, Fitzwilliam | director | 2021-04-01 |

### Resigned within the lookback window

| Name | Role | Appointed | Resigned |
|---|---|---|---|
| TONGS, Tabitha | director | 2021-04-01 | 2026-01-10 |
| BELLOWS, Barnaby | director | 2022-01-05 | 2026-03-22 |
| CRUCIBLE, Cressida | secretary | 2021-04-01 | 2026-06-30 |

## Persons with significant control

| Name | Kind | Natures of control | Notified | Ceased |
|---|---|---|---|---|
| Mrs Arabella Anvil | individual-person-with-significant-control | ownership-of-shares-75-to-100-percent | 2021-04-01 |  |

## Sanctions and PEP screening

Dataset default, threshold 0.7, algorithm best. Scores are identity-match confidence, not risk.

| Subject | Kind | Top score | Result |
|---|---|---|---|
| GILDED ANVIL HOLDINGS LTD | company | 0.55 | below threshold |
| ANVIL, Arabella | officer and psc | no candidates | below threshold |
| FORGE, Fitzwilliam | officer | no candidates | below threshold |

## Network expansion

Current officers' other current appointments (first degree):

- ANVIL, Arabella: 1 current appointment(s) at other companies: AURUM SMELTING LTD (99999803)
- FORGE, Fitzwilliam: 0 current appointment(s) at other companies

No co-appointment overlap among current officers.

Disqualification register checks:

- ANVIL, Arabella (officer and psc): no record matched.
- FORGE, Fitzwilliam (officer): no record matched.

## Adverse media

3 deduplicated result(s) across the query categories. Headlines and snippets stay in the evidence files by design; this memo lists sources only.

| Source domain | Published | Query category | URL |
|---|---|---|---|
| fictional-gazette.example | 2026-05-14 | misconduct | https://fictional-gazette.example/business/gilded-anvil-inquiry |
| made-up-times.example | 2026-07-21 | regulatory | https://made-up-times.example/markets/gilded-anvil-filings |
| examiner-fictional.example | 2026-06-02 | litigation | https://examiner-fictional.example/law/gilded-anvil-claim |

## Filing history

4 filing(s) retrieved of 4 on the register. Filings are inventoried, not parsed.

| Date | Category | Description |
|---|---|---|
| 2025-01-20 | accounts | accounts-with-accounts-type-micro-entity |
| 2025-10-02 | confirmation-statement | confirmation-statement-with-no-updates |
| 2024-05-03 | mortgage | mortgage-create-with-deed |
| 2023-09-01 | change-of-name | change-of-name |

## Charges

| Charge code | Status | Created | Satisfied |
|---|---|---|---|
| 999999010001 | outstanding | 2024-05-01 |  |

## Insolvency

No insolvency cases in this casefile. See the findings for the basis.

---

This memo is a research aid generated from public sources at a point in time. It is not investment advice, not a credit reference, and not a consumer report. Do not use it for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. Officer and PSC data is personal data from public registers; you are responsible for processing it lawfully. Verify everything independently before acting on it.

Note: the clock for this run was overridden through COLDSCREEN_SCREENED_AT, so the screening and retrieval timestamps above were injected, not observed. This is a reproducibility feature for tests and demos.

Contains public sector information licensed under the Open Government Licence v3.0. Source: Companies House.
