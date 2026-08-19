# coldscreen: architecture and build spec

Status: v0.1. The project was named "coldscreen" on 2026-08-19 after the section 16 availability checks; DECISIONS.md carries the dated build and verification record.

## 1. Purpose

Turn a company name into a first-pass screening memo built entirely from public sources, in one command, with every finding traceable to evidence. The target user is anyone who screens inbound companies for a living: corporate finance advisors, VC and angel associates, corp dev teams, compliance analysts, journalists.

## 2. Design principles

1. **Deterministic collection, model synthesis.** Code fetches facts. The model reasons over structured facts and drafts prose. The model never supplies a registry fact from memory; registry data enters the context as fetched JSON or not at all.
2. **Evidence or it did not happen.** A finding without a source URL and retrieval date fails schema validation.
3. **Absence is data.** Empty results are recorded as explicit findings ("no charges registered"), never dropped.
4. **One command, one artifact.** CLI-first. The memo is the product; everything else supports it.
5. **Self-hostable end to end.** Hosted models are optional. Local models via Ollama are first-class, so core logic must not depend on provider-specific features.
6. **Opinionated layer on open plumbing.** Registries, sanctions data, and matching engines already exist in the open. Integrate them; do not rebuild them.

## 3. Related work and positioning

- **zoharbabin/due-diligence-agents**: open-source, multi-agent M&A due diligence over an uploaded data room. Different funnel stage; coldscreen runs before any documents change hands, from public sources only.
- **Commercial screeners** (Scoreplex, Diligent, and similar): hosted SaaS, book-a-demo distribution. coldscreen is self-hostable with a published, versioned methodology.
- **Open plumbing**: Companies House API and existing MCP servers around it, OpenSanctions data and their yente matching server. coldscreen builds on these rather than competing with them.

The differentiation is the methodology (rubric, claims-vs-evidence discipline, memo shape) and its provenance, not the plumbing.

## 4. System overview

```
 input: company name or number (+ optional deck PDF, website URL)
   |
   v
 [1] entity resolution ..... registry search, disambiguation
   |
   v
 [2] registry pass ......... profile, officers, PSC, filings, charges, insolvency
   |
   v
 [3] network expansion ..... officers' other appointments, disqualifications
   |
   v
 [4] sanctions / PEP ....... OpenSanctions match: entity, officers, PSCs
   |
   v
 [5] adverse media ......... structured web search, deduplicated, dated
   |
   v
 [6] claims extraction ..... deck + site -> discrete checkable claims
   |
   v
 [7] synthesis ............. claims vs evidence, rubric, verdict, questions
   |
   v
 [8] render ................ memo.md + evidence bundle in case directory
```

Stages 1 to 5 are deterministic (HTTP + parsing). Stage 6 uses the model for extraction over provided text. Stage 7 is the only reasoning stage. Stage 8 is templating.

## 5. Pipeline stages

**[1] Entity resolution.** Search the registry by name; return candidates (name, number, status, incorporation date, registered address). If more than one plausible candidate, prompt interactively or require `--company-number`. Ambiguity is surfaced, never silently resolved.

**[2] Registry pass.** Fetch company profile, officer list (current plus resigned within the last 5 years, configurable), PSC register, filing history metadata, registered charges, insolvency events. Persist every raw response.

**[3] Network expansion.** For each current officer and PSC: other active appointments and disqualification status. First-degree expansion only in v0.1; deeper graphs are a cost and scope trap.

**[4] Sanctions and PEP.** Match the entity and all individuals from stages 2 and 3 against OpenSanctions. Two integration modes: their hosted API (key required, priced) or a self-hosted yente matching server (verify current setup docs). Record match scores. Sub-threshold results are recorded as "no match above threshold X", with X stated.

**[5] Adverse media.** A fixed query template set per subject: name + fraud, insolvency, regulator, lawsuit, sanctions, plus obvious name variants. Prefer the model provider's native web search tool where the API offers one; otherwise a pluggable `SearchProvider` (Brave, Tavily, or similar; verify current APIs and pricing in USD). Deduplicate, keep top N per query with URL and publication date.

**[6] Claims extraction.** Extract text from the deck (see section 16 on PDF library choice) and from the site's homepage and about pages. The model converts this into discrete `Claim` objects: text, source location, category (history, financials, team, traction, regulatory). Unfalsifiable puffery is listed but tagged out of scope.

**[7] Synthesis.** The model receives the structured `CaseFile` only. It produces: the claims-vs-evidence table, a findings narrative, a verdict citing rubric trigger IDs, and 3 to 5 clarification questions an advisor would actually send. Language rules are baked into the prompt: memos say "contradicted by public record", never "lying"; confidence tags are mandatory.

**[8] Render.** Jinja2 template to `memo.md` (HTML optional later). Case directory layout:

```
cases/acme-holdings-01234567/
  memo.md
  casefile.json
  evidence/
    registry_profile.json
    officers.json
    sanctions_matches.json
    media_001.json
    ...
```

`coldscreen rerun <case-dir>` re-runs synthesis from cached evidence without refetching, so prompt iteration is cheap.

## 6. Data model (Pydantic v2 sketch)

```python
class Evidence(BaseModel):
    source_url: str
    retrieved_at: datetime
    excerpt: str | None = None

class Finding(BaseModel):
    id: str                      # e.g. "REG-003"
    stage: str
    severity: Literal["red", "amber", "info"]
    confidence: Literal["confirmed", "indicated", "unverified"]
    statement: str
    evidence: list[Evidence]     # min length 1, enforced

class Claim(BaseModel):
    id: str
    text: str
    source: str                  # "deck p.4", "site /about"
    category: Literal["history", "financials", "team", "traction", "regulatory", "other"]
    checkable: bool

class ClaimAssessment(BaseModel):
    claim_id: str
    status: Literal["supported", "contradicted", "unverified"]
    basis: list[Evidence]

class Verdict(BaseModel):
    level: Literal["red", "amber", "green"]
    triggered: list[str]         # rubric trigger IDs
    rationale: str
    questions: list[str]

class CaseFile(BaseModel):
    subject: CompanyProfile
    officers: list[Officer]
    findings: list[Finding]
    claims: list[Claim]
    assessments: list[ClaimAssessment]
    verdict: Verdict | None
    tool_version: str
    screened_at: datetime
```

Validation is the enforcement mechanism for design principle 2: a `Finding` with an empty evidence list cannot exist.

## 7. Provider abstractions

```python
class ModelProvider(Protocol):
    def complete(self, system: str, messages: list[Message],
                 json_schema: dict | None = None) -> str: ...

class SearchProvider(Protocol):
    def search(self, query: str, n: int = 5) -> list[SearchResult]: ...
```

Implementations: Anthropic, OpenAI, Ollama (local HTTP). Keep core to plain completion plus JSON-constrained output so local models stay first-class. Do not code SDK calls from memory: verify current method names and parameters in each provider's docs at build time.

## 8. Scoring rubric

The live verdict logic is `rubric.md`, currently version 0.3, versioned with the code. Changing it is a visible, reviewable act. The "fires only when" column in that file is the contract; the code mirrors it, and a test parses the markdown and asserts the mirror. Summary:

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
| A7 | Company status is not active | AMBER |

Rules: any RED trigger forces a RED verdict. Any AMBER trigger yields at least AMBER; AMBER triggers never escalate to RED, however many there are. A verdict citing no triggers must be GREEN. Mechanically detected triggers cannot be dropped by the model, and triggers whose evidence conditions are not met cannot be added by it. The model's judgment operates inside those conditions, never on the level arithmetic.

R4 is hybrid as of version 0.3. A stored claim whose text places the company's origin in a calendar year before the registry incorporation date is a mechanical candidate and cannot be dropped. Other contradictions (for example a debt-free claim against a registered charge) still require a surviving contradicted assessment whose basis includes a registry finding relevant to the claim's category.

## 9. CLI surface

```
coldscreen screen "Acme Holdings Ltd"
coldscreen screen 01234567 --deck pitch.pdf --site https://example.com
coldscreen screen 01234567 --model <provider:model> --json
coldscreen rerun cases/acme-holdings-01234567
coldscreen mcp                                  # MCP stdio server, see section 18
```

Keys via environment variables. Optional `coldscreen.toml` for defaults (model, output dir, officer lookback years, media result count).

Every write into a case directory refuses to follow a symbolic link at the name it is writing (`casedir.write_case_text`, `O_NOFOLLOW`). A validated directory does not imply a validated write: the kernel resolves the final name again at open time, so a link left at `memo.md` would otherwise redirect the write outside the directory the caller was confined to.

## 10. Caching, rate limits, audit trail

- SQLite HTTP cache keyed by URL + params, default TTL 7 days. Reruns are near-free.
- Respect Companies House rate limits (verify the current limits in their docs; do not hardcode a guessed number).
- Every fetch is logged with a timestamp. The case directory doubles as an audit pack: memo plus raw evidence plus tool version.

## 11. Testing

- **Unit**: fixture-based, with recorded (fictional or anonymized) registry JSON.
- **Golden case**: one fully synthetic company with planted contradictions (wrong incorporation date in deck, hidden charge, one sub-threshold sanctions near-match). Snapshot-test the memo.
- **Rubric anchoring**: run the golden CaseFile through two different models; the verdict level must not differ. If it does, the rubric or prompt is underspecified, not the model. As of rubric 0.3 the golden RED case also has a mechanical R4 candidate (origin year before incorporation), so that class of contradiction no longer depends on the model citing it.
- **Integration demo (manual, not CI)**: retroactive screen of an adjudicated collapse using period-appropriate public data, as the launch asset. Wirecard is the obvious candidate; verify what remains accessible via UK-visible records and archives before committing to it.

## 12. Milestones (three weekends)

**Weekend 1: deterministic core.** Repo scaffold, CLI, entity resolution, registry pass, template render with findings only. No model calls. Success test: a real memo for a real UK company, zero LLM involvement.

**Weekend 2: judgment.** Network expansion, sanctions/PEP, adverse media, synthesis with rubric. Success test: coherent AMBER and GREEN verdicts on two live companies, every finding cited.

**Weekend 3: the signature feature and launch.** Claims extraction, claims-vs-evidence table, polish, README examples regenerated from real runs, the retroactive demo, launch assets (memo screenshot, a 90-second terminal recording). Success test: `pip install` to finished memo in under five minutes on a clean machine.

## 13. Non-goals (v0.1)

No UI. No non-UK registries. No financial statement parsing. No people search beyond officers and PSCs. No monitoring or alerting. No data room ingestion. Each of these is a fork in the road that delays shipping the thing only this project does: the public-record screen with a published methodology.

## 14. Licensing decision

Recommendation: **MIT** for the code. Maximum reach, minimum friction, and the strategic goal is credibility and adoption, not moat defense. AGPL was considered (it deters closed SaaS wrapping) but adds adoption friction for exactly the corporate finance users who would spread this. Data licenses are separate and bind the user, not the repo: state this explicitly in the README, especially for OpenSanctions.

## 15. Compliance and privacy notes (not legal advice)

- Research-aid framing appears everywhere a verdict appears, including JSON output.
- FCRA: prohibit use for employment, credit, or tenancy decisions in the docs.
- GDPR: officer and PSC data is personal data from public registers. Legitimate interest is the usual basis for this kind of processing; ship a short `PRIVACY.md`, and any hosted deployment must handle erasure requests.
- Companies House data reuse terms: verify the current terms before launch.
- Defamation posture: memos state record and confidence. Language rules in the synthesis prompt forbid accusatory phrasing; this is a technical control, not just a docs note.
- No scraping behind authentication, ever.

## 16. Verify before build

This checklist was completed on 2026-08-18 and 2026-08-19, before any code was written. Every verified fact is recorded with sources and dates in DECISIONS.md; formal trademark clearance on the final name remains a pre-announcement step tracked by the maintainer.

- [x] Name availability: GitHub, PyPI, and a trademark sanity check on the final name.
- [x] Companies House developer hub: registration flow, key issuance, current rate limits, data reuse terms.
- [x] OpenSanctions: current data license for commercial-context use; hosted API pricing (USD) vs self-hosting yente; confirm yente's current deployment docs.
- [x] Model provider web search tools: current availability, API shape, and pricing in Anthropic and OpenAI docs.
- [x] Fallback search API choice (Brave, Tavily, or similar): current API and pricing.
- [x] PDF text extraction library: PyMuPDF is believed to be AGPL-licensed, which sits badly inside an MIT repo; verify, and default to pdfplumber (believed MIT) if the conflict is real.
- [x] Retroactive demo target: confirm which Wirecard-adjacent records remain publicly retrievable, or pick a UK-adjudicated alternative.

## 17. Open questions

- Adapter interface shape for v0.2 registries (Bolagsverket for Sweden, SEC EDGAR for the US): design after, not before, the UK pass works.
- Remote MCP: Streamable HTTP, an OAuth story, and a hosted deployment. The stdio server is shipped (section 18); everything past the local process boundary is still open, and it is the same question as the hosted demo below.
- Hosted "screen one company in your browser" demo: raises virality, adds cost and abuse surface. Decide after launch signal, not before.

## 18. MCP surface (stdio)

The screen and rerun flows live in `coldscreen.pipeline`, which returns structured results (`ScreenResult`, `RerunResult`) and never exits the process or writes to a stream. `coldscreen.cli` and `coldscreen.mcp_server` are two thin adapters over that one pipeline: the CLI maps results to exit codes and console output, the MCP server maps them to structured tool content and `ToolError`. There is no second implementation of the screen, so the rubric, the language backstop, and the verdict floor and ceiling apply identically on both paths.

```
coldscreen mcp        # stdio transport, stdout is JSON-RPC only

screen_company(query, company_number=None, deck_path=None,
               site_url=None, model=None, overwrite=False)
rerun_case(case_dir, model=None, render_only=False)
```

Properties that are structural rather than incidental:

- **Optional extra.** The `mcp` package is imported lazily inside the `mcp` command, so a plain install imports and runs with the extra absent and the command explains what to install.
- **stdout discipline.** Human status and logs go to stderr on this path; the transport owns stdout.
- **No key surface.** Neither tool schema has a field that could carry key material. Keys are read from the server process environment, and a missing one is a recoverable error naming the variable.
- **No silent disambiguation.** Where the CLI can prompt a person, the MCP path cannot, so an ambiguous name returns `status: "ambiguous"` with the candidate list and writes nothing. The caller re-calls with `company_number`.
- **Confined writes.** `rerun_case` resolves `case_dir` and refuses anything outside the configured output directory, because on this path the path comes from a host rather than a person's shell. Confining the directory is not sufficient by itself: the tool-owned names inside it are written through `casedir.write_case_text`, which opens with `O_NOFOLLOW` so a symbolic link at `memo.md`, `casefile.json`, or an evidence file is refused rather than followed out of the directory. The case directory and `evidence/` are refused if they are links. This applies to every path that writes a case, CLI included.
- **Tests stay offline.** The server is exercised through the SDK's in-memory client under `pytest-socket --disable-socket`, so the MCP surface is covered without a socket.
