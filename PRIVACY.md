# Privacy notes

firstpass processes personal data from public registers: officer and PSC names, appointments, addresses as published by the registry, and disqualification records. This page states how, and what you as the operator are responsible for. It is not legal advice.

- Data is fetched from public sources only, stored locally in the case directory, and, from the synthesis milestone onwards, sent to the model provider you configure. Run a local model via Ollama if case data should never leave your machine.
- The usual lawful basis for this kind of processing under UK and EU data protection law is legitimate interest. You are the controller for your own runs and must make your own assessment.
- Erasure: delete the case directory and the local HTTP cache directory. Nothing else is stored.
- Do not use firstpass for decisions regulated under the US Fair Credit Reporting Act (employment, credit, tenancy screening) or equivalent regimes. It is a research aid.
- Any hosted deployment built on firstpass must handle erasure requests for whatever it stores.
