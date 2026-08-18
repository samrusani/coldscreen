# Scoring rubric

Version 0.1, draft. This file is the verdict logic. It is versioned with the code and cited by trigger ID in every memo, so changing it is a visible, reviewable act. Refine it against real screens, not in the abstract.

## Triggers

| ID | Trigger | Severity |
|----|---------|----------|
| R1 | Sanctions or PEP match (entity or PSC) at or above threshold | RED |
| R2 | Active insolvency event | RED |
| R3 | Disqualified director in current officer set | RED |
| R4 | Material claim directly contradicted by registry record | RED |
| R5 | Undisclosed related-party network across officers | RED |
| A1 | Overdue or irregular filings | AMBER |
| A2 | Wholesale officer changes within 12 months | AMBER |
| A3 | Charge stack inconsistent with stated capital story | AMBER |
| A4 | Central claim unverifiable from any public source | AMBER |
| A5 | Corporate age or scale inconsistent with stated history | AMBER |
| A6 | Substantive adverse media (confirmed source) | AMBER |

## Rules

1. Any RED trigger forces a RED verdict.
2. Two or more AMBER triggers cap the verdict at AMBER, regardless of narrative tone.
3. The verdict block must cite trigger IDs. A verdict that cites no triggers must be GREEN.
4. A verdict is an opinion generated from public sources at a point in time. It is a reason to ask better questions, not a substitute for judgment.
