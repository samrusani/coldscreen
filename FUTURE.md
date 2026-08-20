# FUTURE

Ideas that are out of scope for v0.1. The non-goals in ARCHITECTURE.md section 13 are binding, so good ideas land here instead of in the codebase.

- Registry adapters beyond the UK. The UK pass is done. Official-docs verification on 2026-08-19 (DECISIONS.md) showed neither candidate is a second Companies House, so the adapter interface stays unforced and v0.1 stays UK-only. Do not extract a Protocol from the UK client alone.
  - Sweden, if built in v0.2: Bolagsverket API for valuable datasets (HVD). Number-only identity lookup (POST `/organisationer` with JSON field `identitetsbeteckning`; official examples send a 10-digit organisationsnummer; official FAQ, updated 2026-05-06, says name search is a future improvement with no date). OAuth 2 client credentials after kundanmälan; no bundled credential. No officers, UBO, or charges on this API. SCB `verksamOrganisation` is F-skatt / VAT / employer activity, not Companies House `active`.
  - United States: SEC EDGAR is a filings archive for SEC filers, not a company register. No documented analogue of company status, PSC, charges, or insolvency resources. Not the second registry. A later filings-source adapter remains possible.
- Remote MCP: Streamable HTTP transport, an OAuth story, and a hosted deployment. The stdio server is built (`coldscreen mcp`, ARCHITECTURE.md section 18); everything past the local process boundary is future work and carries the same cost and abuse questions as the hosted demo below.
- MCP resources for case directories, so a host can browse evidence files directly. Deliberately not built with the stdio server: exposing arbitrary files as resources is a much wider surface than two tools, and the memo already comes back in the tool result.
- Hosted "screen one company in your browser" demo. Decide after launch signal, not before.
- Stable JSON output contract for pipelines. The CLI flag ships in v0.1; a versioned schema guarantee is the future work.
- Better deck parsing: tables, charts, image-heavy decks.
- HTML memo render alongside markdown.
- Continuous monitoring and alerting. Explicit non-goal for v0.1.
- Financial statement parsing. Explicit non-goal for v0.1; filings are inventoried, not read.
- People search beyond registered officers and PSCs. Explicit non-goal for v0.1.

Deferred from the pre-launch hardening review:

- Residual: unseen future model phrasing can still miss the stage-honesty phrase set, and an honest gap sentence that uses a clean-claim substring without a not-run marker still fails closed. Observed not-run miss classes are in the tuples; a same-field not-run marker suppresses a clean-claim substring. The set stays deliberately narrow so honest gap sentences still pass.
- Switch the pinned transport off the private httpx `_pool._network_backend` assignment when `HTTPTransport` grows a public constructor hook for a custom httpcore `NetworkBackend`. Until then the assignment stays, the isinstance guard fails closed, and the hermetic HTTP and TLS tests hold the wiring. See the 2026-08-19 DECISIONS entry.

Ideas deferred from the weekend 3 build and review:

- OCR fallback for image-only decks; the gap is currently recorded explicitly as a finding.
- Fuzzy quotation matching for claim verification, with a strict-verbatim default; per-claim provenance offsets into the extracted text for a stronger audit trail.
- Scope the whole-memo language backstop's exemptions to the rendered claims-table region, closing the hand-tampered-casefile residual on render-only reruns.
- A minimum-substance rule for stored claims so single-word quotes cannot become exemption spans.
- Sitemap-based about-page discovery and a configurable path token list for site extraction.
- Label-aware claim re-verification in the language check script (mapping site evidence back to source labels).

Tooling ideas deferred from the weekend 1 build:

- Live Companies House charges page-size maxima, whether the live charges list honors query fields the official spec still omits, and live 429 response headers. The client now walks charges pages and honors Retry-After in tests; those live facts stay UNVERIFIED. Do not trip the production 429 window to find out.
- Document a Companies House sandbox recipe (reachable today via the base URL setting) once a test key exists.
