# Privacy notes

coldscreen processes personal data from public registers: officer and PSC names, appointments, partial dates of birth, nationalities, addresses as published by the registry, and disqualification records. This page states where that data goes and what you as the operator are responsible for. It is not legal advice.

## Where data flows when you run a screen

- **Companies House** receives your queries (company names and numbers). Responses are stored locally.
- **OpenSanctions** (only if you configure `OPENSANCTIONS_API_KEY` or a self-hosted yente URL) receives match queries containing the entity name and, for people, name, month and year of birth, and nationality.
- **Tavily** (only if you configure `TAVILY_API_KEY`) receives adverse media search queries containing subject names.
- **Your model provider** receives the synthesis input document: findings, officer names, roles and dates, extracted claims, media titles and metadata, and sanctions screening summaries. With Anthropic or OpenAI configured, that document leaves your machine. With local Ollama, it does not. Run fully local if your deal flow should never touch a third-party API.
- **Your MCP host**, if you run `coldscreen mcp` and connect it to one, receives whatever the tools return. That includes the memo text, so officer names and other register data from the case appear in the host's conversation and in whatever the host retains of it. With a hosted host that is a third party receiving personal data even when synthesis runs locally on Ollama, which is a separate flow from the model provider line above and worth deciding about separately. The server itself is a local process on your machine and speaks only over stdio to whichever program launched it; it opens no port and no network transport is implemented.
- **A local PDF you did not choose yourself** can enter a screen that way. An MCP host may pass any readable file path as `deck_path`, and whatever text coldscreen can extract from that PDF is persisted into the case directory as evidence, sent to the configured model for claims extraction, and quoted in the memo the host receives. The path is checked (it must exist, be a file, and be readable) but is deliberately not restricted to a directory, because pitch decks do not live in the output directory. Treat connecting a host as granting it the ability to have a local PDF read, stored, and quoted.
- Nothing else receives anything. The tool has no telemetry.

## What is stored locally, and where

- **Case directories** (default `./cases/`): the memo, the casefile, and every raw API response as evidence. These contain personal data from the register and are yours to protect and to delete.
- **The HTTP cache**: full Companies House responses for up to 7 days, in your platform's user cache directory under `coldscreen` (on macOS, `~/Library/Caches/coldscreen/`).

Erasure means deleting both: the case directories you have written, and the cache directory. Cases written by an MCP client land in the same place, under the configured output directory, so they are covered by the same deletion. If you have connected an MCP host, its own conversation history is a third location that only that host can clear.

## Your responsibilities

- The usual lawful basis for this processing under UK and EU data protection law is legitimate interest. You are the controller for your own runs and must make your own assessment.
- Do not use coldscreen for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. It is a research aid.
- OpenSanctions data carries its own licence (CC BY-NC 4.0, with commercial use requiring their paid licence); Companies House data is Open Government Licence v3.0 with personal data carved out. Both bind you, not this repository's MIT licence.
- Any hosted deployment built on coldscreen must handle erasure requests for whatever it stores.
