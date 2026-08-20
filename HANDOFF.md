# coldscreen: builder handoff

For the team taking over development. Read ARCHITECTURE.md and rubric.md before writing anything, and read DECISIONS.md before questioning any design choice: it is a dated record of what was verified, what was decided, and why. If you believe a deviation is justified, raise it and log it in DECISIONS.md with a date. Do not silently redesign.

The repository is at github.com/samrusani/coldscreen. Branch `main` is current and CI is green.

## What this is

A CLI that turns a UK company name into a first-pass screening memo built entirely from public sources, with every finding traceable to evidence. It is not due diligence. It is the screen that decides whether due diligence is worth anyone's time. The same pipeline is also an MCP stdio server, so the screen runs inside agent workflows without a second implementation of it.

Current state: 826 tests, 29 modules, roughly 11,341 lines of source, green on Python 3.11 through 3.13. All three milestone success tests passed, two of them against the live Companies House API. Feature-complete for v0.1; not yet published to PyPI. Rubric 0.3 adds a mechanical R4 floor for origin-year contradictions ("operating since 2015" against incorporation in 2019), so that class of claims-bearing case now anchors unconditionally. Cache UX (`--refresh`, `coldscreen cache path|clear|stats`) landed after charges pagination. `--no-write` on `screen` skips the case directory for that run and still uses the HTTP cache; the default persist path is unchanged. A screen writes `fetch_log.json` at the case directory root (URLs, sanitized params, timestamps, cache flags; no bodies, no keys); rerun and `--no-write` do not. The language check now covers tool-authored casefile statements as well as rendered memos and templates. The stage-honesty phrase set grew from observed not-run lies; the gate is still a substring check, still sanctions and media only, and still arms only when those stages are recorded not run or failed. SNI through the pinned backend is proven by a loopback HTTPS handshake; the httpx pool assignment stays fail-closed by choice.

## The five non-negotiables

Everything else is negotiable with a logged reason. These are not.

**1. Deterministic collection, model synthesis.** Code fetches facts. The model reasons over fetched, structured facts and drafts prose. The model never supplies a registry fact, a sanctions result, or a media item from its own memory: if it was not fetched this run or loaded from cache, it does not exist. Anything that lets model output become a fact in the casefile is a bug, however convenient.

**2. Evidence is enforced in the schema, not in prose.** `Finding.evidence` has minimum length 1, enforced by Pydantic. A finding without a source URL and retrieval timestamp cannot be constructed. Absence is recorded as findings ("no charges registered"), never dropped. Every raw API response is persisted to the case directory, which is an audit pack. `--no-write` is an explicit opt-out of that directory for one run; it does not weaken this default. There are tests that prove an evidence-free finding raises ValidationError; if you ever find yourself deleting one, stop.

**3. The verdict is not the model's to move.** This is the heart of the tool and the part that took the most work to get right. Read `src/coldscreen/rubric.py` and the rubric table in rubric.md together. Two directions, both mechanical:

- The floor: mechanically detected triggers cannot be dropped by the model. If the registry says the company is in liquidation, R2 fires whatever the narrative says.
- The ceiling: triggers whose evidence conditions are not met cannot be added. Every trigger id has a published condition in rubric.md, and the code mirrors that table (a test parses the markdown and asserts the mirror, so the docs cannot drift from the code).

The level is a pure function of the enforced trigger set. There is a property test asserting the level always lands between the candidate-only level and the candidates-plus-open-gates level, across tens of thousands of random model outputs. If you add a trigger, you add its evidence condition to rubric.md, mirror it in the catalog, gate it, and extend that matrix. A trigger without a gate is a hole.

**4. The language control is a technical control, not a tone preference.** Memos say "contradicted by public record" with a confidence tag; they never state or imply fraud, dishonesty, or intent. Three layers: the prompt states the rules, a per-field gate scans model output with one corrective retry, and a whole-memo scan runs before any memo reaches disk. The banned-word scan strips URLs (source URLs legitimately contain words like fraud in slugs) and exempts provenance-verified quoted data.

That exemption is the subtle part and it has already been attacked successfully once. Claim text is the company's own words, so a deck saying "we fight fraud" must render. But claim text comes from the model, so an unverified exemption is a laundering channel: an early design let a model smuggle arbitrary vocabulary into memos by inventing a "quotation". The fix, which you must not weaken: a claim is stored only if it verifies as a normalized verbatim substring of its declared source section, model prose gets zero exemptions ever, and the CI language script re-verifies each claim against its labeled evidence section, not a joined corpus. Single-word quotes are not stored and are not exemption spans. Four attack shapes are permanent regression tests in `tests/test_exemption_attacks.py`. The whole-memo backstop applies claim-quote exemptions only inside the rendered claims-table region. A start heading is accepted only when the next non-blank line is a template-controlled opener, and pre-table model fields are collapsed so they cannot mint that heading. Code-fetched rendered strings (office, network names, media domains, claim source labels) are span-exempt; CI re-verifies each class against sibling evidence. The CI memo scan uses the same claims-table region as the in-process backstop. On an on-disk rerun the in-process backstop re-verifies each stored claim against sibling evidence with the same helper CI uses before treating it as an exemption span; screen and in-memory calls without an evidence dir keep the substance-only filter. `coldscreen check-language` ships with the wheel. This item is done. Recommended next is AUD-004 (track `uv.lock` and `uv sync --locked` in CI); the audit language-control items are closed and remaining work is packaging.

**5. No key material anywhere it can leak.** Secrets come from environment variables only. The Companies House key travels in a basic auth header, never in URLs, cache keys, evidence params, or `__repr__`. There is a test that greps every written file for the key. The tool bundles no OpenSanctions key: their terms make rights non-transferable, so every user brings their own key under their own licence. The MCP surface inherits this rather than reopening it: no tool schema has a field that could carry a key, there is a test that walks both schemas asserting so, and keys are read from the server process environment the host sets.

None of the five is relaxed on the MCP path. It is an adapter over the same `coldscreen.pipeline` functions the CLI calls, so the rubric floor and ceiling, the language backstop, and the evidence schema are the identical code. If you ever find yourself adding a screen path that does not go through the pipeline, that is the bug.

## How the work has been done, and why it is worth continuing

Every milestone followed the same loop, and it caught things that green test suites did not:

1. **Verify before building.** No external API shape is coded from memory. Endpoints, auth, rate limits, licence terms, and library licences get verified against current official documentation, with the source URL and retrieval date recorded in DECISIONS.md. Anything unreachable is marked UNVERIFIED rather than guessed.
2. **Write a work order** before building. The orders live in `wiki/work-orders/` in the original author's private notes; the shape is: scope, hard rules restated verbatim, deliverables, tests required, definition of done.
3. **Build, then review adversarially with a fresh context.** The reviewer did not write the code, re-runs every gate itself rather than trusting the report, and tries to break the guarantees rather than confirm them.
4. **Adjudicate the findings.** Reviewers produce false positives with confidence. Check each finding against the primary documents before acting. Roughly one in ten has been wrong.
5. **Fix, then verify independently.** Run the gates yourself. For anything security-relevant, read the diff and the test rather than trusting a report.

Findings this loop caught that the test suite did not: pagination that silently skipped records when a server clamped page size; the exemption laundering above; a judgment trigger that could fire when the stage feeding it never ran; and an SSRF where the site fetcher validated one DNS resolution while the HTTP client re-resolved at connect time.

**Reviewer checklist, learned the hard way:**

- Attack the gate table itself, not only the gates that exist. Ask of every trigger: under what state can model output make this appear or disappear.
- For any check-then-act on an external resource, ask whether the act re-derives what the check validated. Time-of-check-to-time-of-use bugs are invisible to any test that mocks the transport, because check and connect collapse into one mocked call. The only test that catches them stands up a real socket with a resolver whose answer changes between calls.
- Verify claims about CI against the CI file. A comment in a decisions log saying network isolation is enforced is not enforcement.
- Run `actionlint` on workflow changes. GitHub rejects invalid workflows with no logs, no jobs, and no annotation, and local rehearsal cannot catch it. Env keys are case-insensitive there, so `HTTP_PROXY` and `http_proxy` together invalidate the file.

## Working practices

- Python 3.11 or later, typed throughout, ruff clean, mypy clean.
- Tests never touch the network. This is enforced, not assumed: pytest-socket disables sockets in the pytest config, an autouse fixture refuses real DNS resolution, and CI additionally points proxies at a dead address. Every HTTP path is mocked with respx; fixtures are fictional companies with obviously fictional numbers.
- Five gates must pass before anything ships: `pytest`, `ruff check`, `ruff format --check`, `mypy`, `python scripts/check_language.py`. CI adds a wheel-install smoke job and a blocking `pip-audit`.
- Snapshot fixtures are regenerated through the real pipeline, never hand-edited. If a memo snapshot changes, understand why before accepting it.
- Docs and prompts: plain language, no em dashes. Repository files carry no personal information beyond a GitHub handle, no secrets, and no machine-local paths.
- Official documentation (README, ARCHITECTURE.md, rubric.md, DECISIONS.md) and the internal wiki (gitignored: log, plan, launch, work-orders) both get updated when behavior changes. The wiki is easy to miss because it is not in the tree. The marketing site is not in the repository.
- Do not commit `cases/` (real screening output contains personal data), `.env`, or `website/`.

## Operational facts you will want

**Live APIs.** Companies House allows 600 requests per five minutes, documents no Retry-After header, and does not document pagination maxima (the client uses a conservative client-side throttle, backoff with jitter, and configurable page caps; it advances by received count, never by requested page size). An insolvency 404 means either no insolvency record or no such company, so the client gates that call on the profile's links rather than interpreting the 404.

**Local models.** Synthesis runs against Ollama with no cloud key. On the machine this was built on, `qwen3-coder:30b` and `qwen3.8:27b-mlx` both work and produce identical verdict levels. Reasoning-family models corrupt schema-constrained output when thinking is active: set `COLDSCREEN_OLLAMA_THINK=false` for those. `qwen2.5:7b` fails the language discipline (it writes accusatory vocabulary the gate then rejects), which is the gate working, not a bug to route around. `scripts/anchor_check.py` runs the cross-model anchoring test; `scripts/ollama_smoke.py` runs a single-model synthesis smoke.

**Reruns are cheap.** `coldscreen rerun <case-dir> --model <provider:model>` re-synthesizes from cached evidence without refetching, which is how prompt iteration and model comparison are done without spending API calls. That is the case-directory evidence, not the HTTP cache: `rerun` has no `--refresh` flag.

**HTTP cache.** Registry pages sit in SQLite for up to seven days. `coldscreen screen ... --refresh` skips cache reads and still writes successful 200s back, so the next ordinary screen sees the new pages. `coldscreen cache path`, `cache stats`, and `cache clear` inspect or erase that file and need no API key. `cache clear` refuses if the sqlite path is a symbolic link, so it cannot follow a link to wipe another file. Point operators at `cache path` rather than baking in a machine-local absolute path.

**`--no-write`.** `coldscreen screen ... --no-write` (and MCP `no_write`) skips the case directory for that run only and still uses the HTTP cache. The CLI prints casefile JSON to stdout. `--overwrite` with `--no-write` does not delete an existing directory. `rerun` has no `--no-write`. The default persist path is untouched.

**Fetch log.** `write_case` writes `fetch_log.json` at the case directory root on screen: one row per persisted record, same order as `evidence/index.json`, with sanitized params and `from_cache`. No bodies and no keys. It is written on screen, not on rerun, and not on `--no-write`. A new screen with `--overwrite` rewrites it. `rerun` leaves an existing log byte-identical.

**MCP.** `pip install '.[mcp]'` then `coldscreen mcp` serves `screen_company` and `rerun_case` over stdio. The `mcp` package is imported lazily so a plain install still works and the command tells you what to install. Two things surprise people: stdout is JSON-RPC only, so anything you print for a human on that path must go to stderr; and `rerun_case` refuses a `case_dir` outside the configured output directory, which the CLI does not, because a host chooses that path rather than a person. Note what that confinement does and does not do, because the first version of it got this wrong: validating the directory does nothing about a symbolic link left at `memo.md` inside it, since the kernel resolves that name again when the file is opened. Every case write therefore goes through `casedir.write_case_text`, which opens with `O_NOFOLLOW` and refuses a link at the final name, and the case and `evidence` directories are refused if they are links. If you add a file to the case directory, write it with that function, not with `Path.write_text`. Tests drive the server through the SDK's in-memory client, which works under `pytest-socket --disable-socket` only because `tests/test_mcp_server.py` builds its event loop at import time, before the socket guard is installed. That line has a comment on it; do not move it into a fixture.

**Second-registry research.** Official Bolagsverket HVD and SEC EDGAR pages were read on 2026-08-19. The public record is the DECISIONS.md entries. Quote-level notes live in gitignored wiki research files (`wiki/research/bolagsverket.md`, `wiki/research/sec-edgar.md`); those files are not in git. Neither candidate is a second Companies House. Do not extract a Protocol from the UK client. Do not implement either API until a later v0.2 work order.

## What to build next

FUTURE.md holds remaining items. My recommended ordering:

1. AUD-004: track `uv.lock` and `uv sync --locked` in CI, or launch leftovers.

The CI memo scan now scopes claim-quote exemptions to the claims-table region, same as the in-process backstop. Rerun claim re-verification and the shipped `check-language` command are done: on-disk rerun honors a stored claim only after `claim_quote_is_verified` against sibling `evidence_sections`; screen stays substance-only; `coldscreen check-language` is in the wheel and the package job runs it on the demo-case memo. Heading injection and code-fetched rendered strings are done: pre-table fields collapse to one line, the claims-table start requires a template opener, and office / network names / media domains / claim source labels are span-exempt after CI evidence re-verify. Label-aware CI re-verification is done: each claim is checked against its declared source label (`deck p.{key}`, `site {path}` from the requested URL), not a joined evidence corpus. Missing source, unknown label, or a hit only in a different section buys no exemption. The minimum-substance rule is done: stored claims must have two or more tokens after `normalize_for_match`; a single-word quote is dropped (EXT-009) and is not an exemption span even if a hand-edited casefile still contains it. The claims-table-region scope is done: the whole-memo backstop applies claim-quote exemptions only between `## Claims vs evidence` and the next `## ` heading, and fails closed when that region cannot be bounded. The per-run fetch log is done: `fetch_log.json` is written on screen, not on rerun, and not on `--no-write`. The evidence index stays a manifest. Duration and retry rows were not added. The five non-negotiables above are untouched.

The language check now covers casefile statements: CI parses `casefile.json` and scans the tool-authored fields (finding statements, record notes, verdict rationale and questions, narrative, enforcement notes, skipped reasons). Media titles and claim texts are not scanned. Identity exemptions still require sibling registry evidence; claim-quote exemptions do not apply to those fields.

`--no-write` for JSON-only `screen` is done: the case directory is skipped for that run, the HTTP cache still stores 200s, MCP `case_dir` is JSON null, and the default persist path is untouched. Registry adapters for other jurisdictions were verified on 2026-08-19 and recorded in DECISIONS.md: neither FUTURE candidate is a second Companies House, so no Protocol was extracted and no Swedish or EDGAR client was added. v0.1 stays UK-only. If a later sprint builds Swedish identity lookup, use Bolagsverket HVD (number-only, OAuth after kundanmälan, no officers / UBO / charges). EDGAR is a filings archive for SEC filers, not a register.

The hermetic TLS SNI test now proves the pinned backend preserves server-observed SNI on a loopback HTTPS handshake, not only the Host header on plain HTTP. The httpx `_pool._network_backend` assignment stays fail-closed by choice until a public constructor hook exists; see the dated DECISIONS.md entry. The stage-honesty phrase set grew from observed not-run lies (PEP-first order, evidence-of phrasing, and the `ran and returned no` leak). It is still a substring check, still sanctions and media only, and still arms only when those stages are recorded not run or failed. A clean-claim substring in the same field as a not-run marker (`not performed`, `did not run`) is not treated as a clean result; a marker in another field does not excuse a lying narrative; citing SAN-000 is not a marker. Unseen future phrasing remains a residual by design. The five non-negotiables above are untouched.

## Known open items that are not code

- PyPI publication (the name is unclaimed and gets claimed at first publish).
- Formal trademark clearance on "coldscreen" in the finance and compliance classes. Availability research was done; a real register search was not.
- An email to OpenSanctions confirming the open-source-client licensing reading (users bring their own key). The current README states the conservative interpretation.
- The domain decision.

## The shortest version

Fetch facts in code. Constrain the model. Enforce the verdict mechanically. Persist everything. Say only what the record supports. If a change makes any of those weaker, it is the wrong change however much it improves the demo.
