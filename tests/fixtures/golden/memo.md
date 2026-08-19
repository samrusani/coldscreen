# Screening memo: FABRICATED WIDGETS LTD

Company 99999999 (england-wales). Screened 2026-08-18 12:00 UTC. Tool version 0.1.0.dev0.

Registered office: 1 Imaginary Way, Faketown, ZZ99 9ZZ, England
SIC codes: 28990

## Verdict

**RED**

Triggered:

- **R4** (RED): Material claim directly contradicted by registry record
- **A1** (AMBER): Overdue or irregular filings
- **A2** (AMBER): Wholesale officer changes within 12 months
- **A4** (AMBER): Central claim unverifiable from any public source

R4 applies because material claims are directly contradicted by the registry record: the operating-since claim (CLM-001) against the incorporation date (REG-002), and the debt-free claim (CLM-002) against the outstanding charge (REG-006). A1 applies because the registry marks the next accounts overdue (REG-003). A2 applies because officer resignations in the last twelve months met the wholesale-change threshold (REG-009). A4 applies because the team-size claim (CLM-004), central to the deck's capability story, is unverifiable from any source in this casefile. Any RED trigger forces a RED verdict.

Synthesis: provider ollama, model fake-model:1b, prompt version 3, rubric-enforced.

This memo is a research aid generated from public sources at a point in time. It is not investment advice, not a credit reference, and not a consumer report. Do not use it for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. Officer and PSC data is personal data from public registers; you are responsible for processing it lawfully. Verify everything independently before acting on it.

## Claims vs evidence

What the company says about itself, against the public record. Claim text is quoted verbatim from the deck or site named in the source; the status column is the enforced assessment.

| # | Claim (source) | Public record | Status |
|---|---|---|---|
| 1 | "Operating since 2015 with a national footprint" (deck p.2) | Incorporated on 2019-05-14 per the registry profile, four years after the claimed start. | Contradicted |
| 2 | "The company is debt free and self funded" (deck p.2) | The charges register lists 2 charges, 1 outstanding. | Contradicted |
| 3 | "Our platform eliminates fraud in widget procurement" (deck p.2) |  | not checkable |
| 4 | "A team of 40 widget engineers" (deck p.3) | No public source in this casefile states headcount. | Unverified |
| 5 | "The most trusted name in widgets" (deck p.3) |  | not checkable |

## Narrative

FABRICATED WIDGETS LTD is an active company incorporated on 2019-05-14 (REG-001, REG-002). Five claims were extracted from its deck (EXT-001, EXT-006), and two of the checkable ones do not survive contact with the register. The deck's operating-since assertion (CLM-001) sits four years before the incorporation date on the registry profile (REG-002, confirmed), and the debt-free assertion (CLM-002) is contradicted by the charges register, which lists two charges with one still outstanding (REG-006, confirmed). The headcount assertion (CLM-004) has no public source in this casefile and stays unverified. Beyond the claims, the registry marks the next accounts as overdue (REG-003, confirmed), and two of five registered officers resigned in the twelve months before the screening date against three currently active, which meets the wholesale-change threshold (REG-009, confirmed). Network expansion found one co-appointment overlap among current officers (NET-002) and no disqualification records (NET-101, NET-102, NET-103). Sanctions screening and the adverse media search were not performed this run (SAN-000, MED-000), so those absences are open questions rather than clean results.

## Clarification questions

1. Reconcile the deck's operating-since-2015 statement (CLM-001) with the 2019-05-14 incorporation date on the register: was there a predecessor entity, and if so which one?
2. Reconcile the deck's debt-free statement (CLM-002) with the outstanding charge registered in March 2024 (REG-006): what does that charge secure and what is its current balance?
3. What supports the team-of-40 figure (CLM-004): payroll headcount, contractors, or something else?
4. When will the overdue accounts for the period ending 2025-05-31 be filed, and what caused the delay (REG-003)?
5. What led two officers to resign within twelve months (REG-009), and how have their duties been reassigned?

## Findings

### Red

None recorded.

### Amber

- **REG-003** (confirmed) The registry marks the next accounts as overdue.
  - Evidence: https://api.company-information.service.gov.uk/company/99999999 (retrieved 2026-08-18)
- **REG-009** (confirmed) Wholesale officer changes: 2 officer(s) resigned in the 12 months before the screening date, against 3 currently active.
  - Evidence: https://api.company-information.service.gov.uk/company/99999999/officers (retrieved 2026-08-18)

### Info

- **REG-001** (confirmed) Company status is active.
  - Evidence: https://api.company-information.service.gov.uk/company/99999999 (retrieved 2026-08-18)
- **REG-002** (confirmed) Incorporated on 2019-05-14 (7 full years before the screening date).
  - Evidence: https://api.company-information.service.gov.uk/company/99999999 (retrieved 2026-08-18)
- **REG-006** (confirmed) Charges register: 2 charge(s) listed (1 outstanding, 1 satisfied).
  - Evidence: https://api.company-information.service.gov.uk/company/99999999/charges (retrieved 2026-08-18)
- **REG-007** (confirmed) No insolvency history is registered. The company profile offers no insolvency link, so the insolvency resource was not queried.
  - Evidence: https://api.company-information.service.gov.uk/company/99999999 (retrieved 2026-08-18)
- **REG-008** (confirmed) 3 active officer(s) on the register.
  - Evidence: https://api.company-information.service.gov.uk/company/99999999/officers (retrieved 2026-08-18)
- **REG-010** (confirmed) The company has 1 previous name(s) on the register: FICTIONAL WIDGETS LTD.
  - Evidence: https://api.company-information.service.gov.uk/company/99999999 (retrieved 2026-08-18)
- **REG-011** (confirmed) 1 PSC entry(ies) on the register, 1 not ceased. Natures of control: ownership-of-shares-75-to-100-percent, voting-rights-75-to-100-percent.
  - Evidence: https://api.company-information.service.gov.uk/company/99999999/persons-with-significant-control (retrieved 2026-08-18)
- **NET-001** (confirmed) Network expansion covered 3 current officer(s): 2 current appointment(s) at other companies were found.
  - Evidence: https://api.company-information.service.gov.uk/officers/fictOfficer001/appointments (retrieved 2026-08-18)
  - Evidence: https://api.company-information.service.gov.uk/officers/fictOfficer002/appointments (retrieved 2026-08-18)
  - Evidence: https://api.company-information.service.gov.uk/officers/fictOfficer003/appointments (retrieved 2026-08-18)
- **NET-002** (confirmed) Co-appointment overlap: 1 other company(ies) where two or more current officers of the subject hold current appointments: IMAGINARY COMPONENTS LTD (99999801): COGWHEEL, Cornelius, WIDGETSMITH, Wanda.
  - Evidence: https://api.company-information.service.gov.uk/officers/fictOfficer001/appointments (retrieved 2026-08-18)
  - Evidence: https://api.company-information.service.gov.uk/officers/fictOfficer002/appointments (retrieved 2026-08-18)
  - Evidence: https://api.company-information.service.gov.uk/officers/fictOfficer003/appointments (retrieved 2026-08-18)
- **NET-101** (confirmed) No disqualification record matches WIDGETSMITH, Wanda (officer and PSC) on the disqualified officers register.
  - Evidence: https://api.company-information.service.gov.uk/search/disqualified-officers (retrieved 2026-08-18)
- **NET-102** (confirmed) No disqualification record matches COGWHEEL, Cornelius on the disqualified officers register.
  - Evidence: https://api.company-information.service.gov.uk/search/disqualified-officers (retrieved 2026-08-18)
- **NET-103** (confirmed) No disqualification record matches SPROCKET, Sybil on the disqualified officers register.
  - Evidence: https://api.company-information.service.gov.uk/search/disqualified-officers (retrieved 2026-08-18)
- **SAN-000** (confirmed) Sanctions screening not performed: no OpenSanctions key or endpoint configured. Subjects were not screened against any sanctions or PEP dataset this run.
  - Evidence: coldscreen:not-run/sanctions (retrieved 2026-08-18)
- **MED-000** (confirmed) Adverse media search not performed: no search API key configured. Public web coverage was not screened this run.
  - Evidence: coldscreen:not-run/media (retrieved 2026-08-18)
- **EXT-001** (confirmed) Deck ingested: deck_fabricated_widgets.pdf, 3 page(s), text extracted from 3 page(s) (sha256 f6878a47d938d4238f4b06bf2522e9a172b610244397260a4e8cdad755c47d5f).
  - Evidence: coldscreen:deck/deck_fabricated_widgets.pdf (retrieved 2026-08-18)
- **EXT-006** (confirmed) Claims extraction produced 5 claim(s) from 3 text section(s): 3 checkable, 2 not checkable (puffery is listed, never dropped).
  - Evidence: coldscreen:deck/deck_fabricated_widgets.pdf (retrieved 2026-08-18)

## Officers

### Current

| Name | Role | Appointed |
|---|---|---|
| WIDGETSMITH, Wanda | director | 2019-05-14 |
| COGWHEEL, Cornelius | director | 2021-03-01 |
| SPROCKET, Sybil | secretary | 2019-05-14 |

### Resigned within the lookback window

| Name | Role | Appointed | Resigned |
|---|---|---|---|
| FLYWHEEL, Ferdinand | director | 2020-01-15 | 2026-03-01 |
| GASKET, Griselda | director | 2019-06-01 | 2026-05-15 |

## Persons with significant control

| Name | Kind | Natures of control | Notified | Ceased |
|---|---|---|---|---|
| Ms Wanda Widgetsmith | individual-person-with-significant-control | ownership-of-shares-75-to-100-percent; voting-rights-75-to-100-percent | 2019-05-14 |  |

## Sanctions and PEP screening

Not performed: no OpenSanctions key or endpoint configured. Absence of screening is data, not a clean result. See finding SAN-000.

## Network expansion

Current officers' other current appointments (first degree):

- WIDGETSMITH, Wanda: 1 current appointment(s) at other companies: IMAGINARY COMPONENTS LTD (99999801)
- COGWHEEL, Cornelius: 1 current appointment(s) at other companies: IMAGINARY COMPONENTS LTD (99999801)
- SPROCKET, Sybil: 0 current appointment(s) at other companies

Co-appointment overlap (companies shared by two or more current officers):

- IMAGINARY COMPONENTS LTD (99999801): COGWHEEL, Cornelius, WIDGETSMITH, Wanda

Disqualification register checks:

- WIDGETSMITH, Wanda (officer and psc): no record matched.
- COGWHEEL, Cornelius (officer): no record matched.
- SPROCKET, Sybil (officer): no record matched.

## Adverse media

Not performed: no search API key configured. See finding MED-000.

## Filing history

4 filing(s) retrieved of 4 on the register. Filings are inventoried, not parsed.

| Date | Category | Description |
|---|---|---|
| 2026-05-16 | officers | termination-director-company-with-name-termination-date |
| 2025-02-20 | accounts | accounts-with-accounts-type-micro-entity |
| 2024-03-05 | mortgage | mortgage-create-with-date-charge-number |
| 2019-05-14 | incorporation | incorporation-company |

## Charges

| Charge code | Status | Created | Satisfied |
|---|---|---|---|
| 999999990002 | outstanding | 2024-03-01 |  |
| 999999990001 | fully-satisfied | 2020-07-01 | 2023-01-15 |

## Insolvency

No insolvency cases in this casefile. See the findings for the basis.

---

This memo is a research aid generated from public sources at a point in time. It is not investment advice, not a credit reference, and not a consumer report. Do not use it for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. Officer and PSC data is personal data from public registers; you are responsible for processing it lawfully. Verify everything independently before acting on it.

Note: the clock for this run was overridden through COLDSCREEN_SCREENED_AT, so the screening and retrieval timestamps above were injected, not observed. This is a reproducibility feature for tests and demos.

Contains public sector information licensed under the Open Government Licence v3.0. Source: Companies House.
