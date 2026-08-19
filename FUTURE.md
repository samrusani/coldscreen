# FUTURE

Ideas that are out of scope for v0.1. The non-goals in ARCHITECTURE.md section 13 are binding, so good ideas land here instead of in the codebase.

- Registry adapters beyond the UK: Bolagsverket (Sweden), SEC EDGAR (US). Design the adapter interface after the UK pass works, not before.
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

- Extend the stage-honesty phrase set (the mechanical backstop that fails closed when a narrative calls a not-run or failed sanctions or media stage clean) from real model phrasing observed in live runs. The current set is fixed and deliberately narrow to avoid false positives.
- Add a hermetic TLS test for SNI preservation through the pinned network backend. Preservation is proven by design (httpcore derives server_hostname from the origin, which the pin never touches) and by a plain-HTTP Host-header test, but no HTTPS handshake test exists yet.
- Revisit the httpx-internals coupling in the pinned transport if httpx changes its connection-pool shape. It is pinned to the tested versions and fails closed (refuses to run the site stage) otherwise; a public-API path would remove the coupling.

Ideas deferred from the weekend 3 build and review:

- OCR fallback for image-only decks; the gap is currently recorded explicitly as a finding.
- Fuzzy quotation matching for claim verification, with a strict-verbatim default; per-claim provenance offsets into the extracted text for a stronger audit trail.
- Scope the whole-memo language backstop's exemptions to the rendered claims-table region, closing the hand-tampered-casefile residual on render-only reruns.
- A minimum-substance rule for stored claims so single-word quotes cannot become exemption spans.
- Sitemap-based about-page discovery and a configurable path token list for site extraction.
- Label-aware claim re-verification in the language check script (mapping site evidence back to source labels).

Tooling ideas deferred from the weekend 1 build:

- A --no-write mode for pure JSON pipeline use of screen.
- A per-run fetch log file in the case directory, beyond the evidence index timestamps.
- Extend the language check to casefile.json statements, not only rendered memos and templates.
- Live Companies House charges page-size maxima, whether the live charges list honors query fields the official spec still omits, and live 429 response headers. The client now walks charges pages and honors Retry-After in tests; those live facts stay UNVERIFIED. Do not trip the production 429 window to find out.
- Document a Companies House sandbox recipe (reachable today via the base URL setting) once a test key exists.
