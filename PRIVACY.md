# Privacy notes

coldscreen processes personal data from public registers: officer and PSC names, appointments, partial dates of birth, nationalities, addresses as published by the registry, and disqualification records. This page states where that data goes and what you as the operator are responsible for. It is not legal advice.

## Where data flows when you run a screen

- **Companies House** receives your queries (company names and numbers). Responses are stored locally.
- **OpenSanctions** (only if you configure `OPENSANCTIONS_API_KEY` or a self-hosted yente URL) receives match queries containing the entity name and, for people, name, month and year of birth, and nationality.
- **Tavily** (only if you configure `TAVILY_API_KEY`) receives adverse media search queries containing subject names.
- **Your model provider** receives the synthesis input document: findings, officer names, roles and dates, extracted claims, media titles and metadata, and sanctions screening summaries. With Anthropic or OpenAI configured, that document leaves your machine. With local Ollama, it does not. Run fully local if your deal flow should never touch a third-party API.
- Nothing else receives anything. The tool has no telemetry.

## What is stored locally, and where

- **Case directories** (default `./cases/`): the memo, the casefile, and every raw API response as evidence. These contain personal data from the register and are yours to protect and to delete.
- **The HTTP cache**: full Companies House responses for up to 7 days, in your platform's user cache directory under `coldscreen` (on macOS, `~/Library/Caches/coldscreen/`).

Erasure means deleting both: the case directories you have written, and the cache directory.

## Your responsibilities

- The usual lawful basis for this processing under UK and EU data protection law is legitimate interest. You are the controller for your own runs and must make your own assessment.
- Do not use coldscreen for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. It is a research aid.
- OpenSanctions data carries its own licence (CC BY-NC 4.0, with commercial use requiring their paid licence); Companies House data is Open Government Licence v3.0 with personal data carved out. Both bind you, not this repository's MIT licence.
- Any hosted deployment built on coldscreen must handle erasure requests for whatever it stores.
