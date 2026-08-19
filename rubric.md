# Scoring rubric

Version 0.3. This file is the verdict logic. It is versioned with the code and cited by trigger ID in every memo, so changing it is a visible, reviewable act. Version 0.3 adds a mechanical floor to R4 for date-shaped origin claims. Version 0.2 refined version 0.1 against real screens: a status trigger was added after a live screen showed a dissolved company could reach GREEN, and the evidence conditions column makes each trigger's mechanical gate explicit.

## Triggers

| ID | Trigger | Severity | Fires only when |
|----|---------|----------|-----------------|
| R1 | Sanctions or PEP match (entity or PSC) at or above threshold | RED | A sanctions match finding at or above threshold exists for the entity or a PSC. Officer matches are reported as amber findings, not R1. |
| R2 | Active insolvency event | RED | Insolvency cases are on the register, or the company status is itself an insolvency state (administration, liquidation, receivership, insolvency proceedings, voluntary arrangement). |
| R3 | Disqualified director in current officer set | RED | A strong-match disqualification (name and date of birth, or name and company number for corporate officers) that is currently active. |
| R4 | Material claim directly contradicted by registry record | RED | A surviving contradicted claim assessment whose basis includes a registry finding relevant to the claim's category, or a stored claim whose text places the company's origin in a calendar year before the registry incorporation date. |
| R5 | Undisclosed related-party network across officers | RED | Co-appointment overlap findings exist and claims material is present to judge disclosure against. |
| A1 | Overdue or irregular filings | AMBER | The registry marks accounts or the confirmation statement overdue. |
| A2 | Wholesale officer changes within 12 months | AMBER | Resignations in the last 12 months meet the wholesale threshold. |
| A3 | Charge stack inconsistent with stated capital story | AMBER | Charges exist on the register and at least one checkable financials claim was extracted. |
| A4 | Central claim unverifiable from any public source | AMBER | At least one checkable claim was assessed unverified by the model itself. |
| A5 | Corporate age or scale inconsistent with stated history | AMBER | At least one checkable history claim was extracted. |
| A6 | Substantive adverse media (confirmed source) | AMBER | The media stage ran and returned at least one item. |
| A7 | Company status is not active | AMBER | The registered status is dissolved, closed, converted-closed, or removed. Insolvency states escalate to R2 instead. |

## Rules

1. Any RED trigger forces a RED verdict.
2. Any AMBER trigger yields at least AMBER. AMBER triggers never escalate to RED, however many there are.
3. The verdict block must cite trigger IDs. A verdict that cites no triggers must be GREEN.
4. Mechanically detected triggers cannot be dropped by the model, and triggers whose evidence conditions are not met cannot be added by it. The model's judgment operates inside those conditions, never on the level arithmetic.
5. A verdict is an opinion generated from public sources at a point in time. It is a reason to ask better questions, not a substitute for judgment.
