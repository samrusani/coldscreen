# DECISIONS

Dated record of every deviation from the spec and every verified fact from the ARCHITECTURE.md section 16 checklist. If a choice is not obvious from the spec, it is written down here.

## Process decisions

### 2026-08-18: local git only, no remote yet
The repository is initialized locally on branch main. No GitHub repository, no package registration, no name reservation anywhere until the maintainer opens the project up.

### 2026-08-18: the builder handoff stays out of the public tree
The handoff notes are build-process instruction, not user documentation. They live in the internal wiki, which is excluded from git. ARCHITECTURE.md and README.md are the public documents.

### 2026-08-18: MIT licence with a neutral copyright line
LICENSE names "firstpass contributors" as the holder. Repository files carry no personal names, emails, or machine-local paths. The maintainer's GitHub handle can be added at launch.

### 2026-08-18: README maintainer contact reduced to a placeholder
The draft README carried a personal name and email address. Replaced with a placeholder pending launch, per the rule that repo files hold no personal information beyond a GitHub username.

### 2026-08-18: Python 3.11 floor, developed on 3.12
The handoff requires 3.11+. The build machine runs 3.12; CI should exercise 3.11, 3.12, and 3.13.

### 2026-08-18: weekend 1 live success test deferred until a key exists
No Companies House API key is available in the build environment, and key registration requires an account only the maintainer can create. All code and tests run on recorded, fictional fixtures. The "real memo for a real UK company" test runs the moment a key is provided; nothing in the design depends on waiting.

## Build decisions

### 2026-08-18: weekend 1 built, adversarially reviewed, fixed, and verified
The deterministic core was implemented by a builder agent against a versioned work order, then reviewed by an independent agent with a fresh context. The review confirmed every quality gate independently and produced 20 findings; 17 were accepted and fixed, 2 were false positives (adjudicated against the primary documents), and 1 concerned a working tree edit made by the orchestrator, not the builder. The one substantive bug: pagination advanced by the requested page size rather than the received count, which would have silently skipped records if the server ever clamps page sizes (the maxima are officially undocumented). Fixed, with a regression test simulating a clamping server. Final state verified independently: 135 tests green on Python 3.11, 3.12, and 3.13, ruff and mypy clean, language check clean, zero network in tests, offline rerun demo byte-stable.

Decisions made during the fix pass, with reasons:
- The disambiguation exit code is 3, not 2, because typer and click reserve 2 for usage errors and scripts must be able to tell the cases apart.
- A unique exact-title match proceeds without a picker, but the choice is surfaced on stderr with the number of candidates set aside. A unique match on the exact registered name is determinate rather than ambiguous; the notice keeps the spec's "ambiguity is surfaced" principle intact.
- The PSC fetch is gated on the profile's PSC link, mirroring charges and insolvency, because unlinked PSC resources can 404. An absent link produces an absence finding with confidence "indicated" (the register offered no link), while a fetched empty register stays "confirmed".
- Non-200 responses on linked or list resources are persisted as evidence files with their status codes, so the audit pack shows, for example, the insolvency 404 itself. Terminal errors that abort a screen write no case directory, so they have no audit pack to appear in.
- The test clock seam (FIRSTPASS_SCREENED_AT) is now visible: the casefile carries a clock_override flag and the memo footer states when timestamps were injected. An audit pack cannot be silently backdated with one environment variable.
- The fixture company number moved from 09999999 to 99999999 because the former falls in an allocated Companies House range and could be pasted against the live API.
- uv.lock is not tracked. This is a distributable package, CI installs from pyproject, and the lockfile would only add churn.

## Section 16 verification log

Findings are recorded here as verification completes, each with source URL and retrieval date.

### 2026-08-18: Companies House API verified; section 16 checklist complete
Base URL api.company-information.service.gov.uk; HTTP basic auth with the API key as username and blank password; keys come from a live application on the developer hub (a sandbox with test keys exists at api-sandbox.company-information.service.gov.uk). Endpoints for search, profile, officers, PSC, filing history, charges, insolvency, officer appointments, and disqualifications confirmed against the official reference. Rate limit is documented as 600 requests per five-minute window with 429 responses for the remainder of the window; no Retry-After or rate limit headers are documented, so the client uses a conservative client-side throttle plus tenacity exponential backoff with jitter. Two design consequences recorded: the insolvency endpoint's 404 means either "no insolvency record" or "company not found", so the client decides whether to call it from the profile's links.insolvency field (same pattern for charges via links.charges, since has_charges and has_insolvency_history are deprecated); and pagination defaults and maxima are not officially documented, so page size and page caps are configuration with explicit truncation recording, not hardcoded constants. Data reuse: Crown copyright under the Open Government Licence v3.0, attribution line "Contains public sector information licensed under the Open Government Licence v3.0." goes in the memo footer and README; personal data is carved out of OGL and remains the data user's responsibility; the register is not to be used for bulk downloads, which this tool's per-company lookups respect. Sources: developer.company-information.service.gov.uk (get-started, authentication, developer-guidelines, api-testing), developer-specs.company-information.service.gov.uk (guides/authorisation, guides/rateLimiting, public data API reference), gov.uk data products and public task pages, nationalarchives.gov.uk OGL v3, all retrieved 2026-08-18. Full endpoint and schema notes live in the internal research notes. With this, every item on the ARCHITECTURE.md section 16 checklist except the maintainer-owned final name decision is verified and recorded.

### 2026-08-18: OpenSanctions verified; users bring their own licence and key
Data is CC BY-NC 4.0; OpenSanctions states that use inside a for-profit business, including compliance screening that earns no revenue, requires a paid licence. Their API terms (version 202509, section 7.4) make rights non-transferable and non-sublicensable. Consequences for this tool: no bundled or shared API key ever; each user supplies their own key under their own licence; the README keeps stating that the data licence is separate from this repository's MIT licence and binds the user. API facts: base URL api.opensanctions.org, auth header "Authorization: ApiKey", POST /match/{dataset} with all property values as lists of strings, score is identity confidence in 0..1, threshold default 0.7 with 0.8 to 0.85 suggested for fewer false positives, algorithm default "best" currently resolving to logic-v2 (evidence records the resolved name, since "best" drifts). Pricing is published in EUR only (0.10 EUR per match query); no USD figure exists, so our docs must not state one. Self-hosted yente is MIT, deployed via Docker Compose (yente 5.5.0 with Elasticsearch 9.4.2 in the official compose file), exposes the same API surface, so the client needs one configurable base URL. Dataset for sanctions plus PEP: "default". Sources: opensanctions.org/licensing, /docs/api/*, /docs/terms/api/202509, yente.followthemoney.tech, github.com/opensanctions/yente, all retrieved 2026-08-18. Open item for launch: confirm the open-source-client licensing reading with OpenSanctions directly.

### 2026-08-18: PDF extraction library is pdfplumber
PyMuPDF is confirmed dual-licensed AGPL-3.0 / Artifex commercial (pypi.org/pypi/PyMuPDF/json, artifex.com/licensing, retrieved 2026-08-18). Artifex explicitly restricts server-side deployment without AGPL disclosure of the embedding application, which conflicts with this repository's MIT grant in exactly the way the handoff suspected. pdfplumber 0.11.10 is MIT with a fully permissive dependency chain (pdfminer.six MIT, pypdfium2 BSD/Apache) and actively maintained (release 2026-06-15). pypdf 6.16.1 (BSD-3-Clause) is the recorded fallback. Decision: pdfplumber.

### 2026-08-18: the working name cannot survive to launch
Availability research (retrieved 2026-08-18): the PyPI name "firstpass" is taken by an actively developed package (latest release 2026-08-13); the GitHub orgs "firstpass" and "FirstPassTech" exist; RiskScout markets a commercial KYC/KYB product named "FirstPass" that includes sanctions checks and adverse media monitoring, which is this tool's exact category; one registered US trademark "FIRSTPASS" exists in an unrelated class, and two further marks could not be verified because the trademark record sites returned errors. "firstpass-screen" is currently unregistered on PyPI, but launching under a name that collides with a commercial product in the same category is not recommended. Decision: "firstpass" remains a strictly local working name. A final name, a fresh availability pass, and a proper trademark search are launch blockers owned by the maintainer. Nothing has been registered or published anywhere.

### 2026-08-18: fallback search APIs verified, Tavily provisionally first
Brave Search API: GET api.search.brave.com/res/v1/web/search, X-Subscription-Token auth, 5 USD per 1000 requests, free credits require a card. Tavily: POST api.tavily.com/search, Bearer auth, 0.008 USD per credit (basic search 1 credit), free tier 1000 credits per month with no card, topic and time_range parameters fit adverse media queries. Provisional call: implement Tavily first behind the SearchProvider protocol because the card-free tier keeps contributor setup friction low; Brave stays documented as the volume alternative. Final call when the adverse media stage is built. Sources: brave.com/search/api, docs.tavily.com, retrieved 2026-08-18.

### 2026-08-18: model provider APIs verified (used from the synthesis milestone onward)
- Anthropic: `anthropic` Python SDK, `client.messages.create` for plain completion; JSON schema constrained output via `output_config={"format": {"type": "json_schema", "schema": ...}}`. Native web search exists as a server tool (`web_search_20260209`) priced at 10 USD per 1000 searches plus content tokens. Current model IDs: `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`. Verified against the current bundled Anthropic API reference, 2026-08-18.
- OpenAI: `openai` Python SDK v3.2.0, Responses API is the recommended surface (`client.responses.create`); JSON schema output via `text={"format": {"type": "json_schema", ...}}`. Native web search tool `{"type": "web_search"}`, 10 USD per 1000 calls plus content tokens. Docs host moved to developers.openai.com. Note: SDK depends on `httpx2`, check coexistence with our `httpx` pin before adding it. Sources: developers.openai.com/api/docs (structured-outputs, tools-web-search, models, pricing), retrieved 2026-08-18.
- Ollama: local HTTP `POST /api/chat` on `http://localhost:11434`; the `format` field accepts a full JSON schema directly; response text arrives as a JSON-encoded string in `message.content`. OpenAI-compatible `/v1` endpoint exists but its JSON schema fidelity is unverified, so v0.1 keeps a dedicated Ollama implementation. Source: github.com/ollama/ollama docs tree, retrieved 2026-08-18.
- Consequence for the `ModelProvider` protocol: unchanged from ARCHITECTURE.md section 7. One plain JSON schema dict from core; each provider implementation translates to its own dialect. Schemas are generated with `additionalProperties: false` and full `required` lists so a single schema works across all three providers.
