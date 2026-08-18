# FUTURE

Ideas that are out of scope for v0.1. The non-goals in ARCHITECTURE.md section 13 are binding, so good ideas land here instead of in the codebase.

- Registry adapters beyond the UK: Bolagsverket (Sweden), SEC EDGAR (US). Design the adapter interface after the UK pass works, not before.
- MCP server mode, so the screen runs inside agent workflows. Candidate for v0.1.5 once the core is a library.
- Hosted "screen one company in your browser" demo. Decide after launch signal, not before.
- Stable JSON output contract for pipelines. The CLI flag ships in v0.1; a versioned schema guarantee is the future work.
- Better deck parsing: tables, charts, image-heavy decks.
- HTML memo render alongside markdown.
- Continuous monitoring and alerting. Explicit non-goal for v0.1.
- Financial statement parsing. Explicit non-goal for v0.1; filings are inventoried, not read.
- People search beyond registered officers and PSCs. Explicit non-goal for v0.1.
