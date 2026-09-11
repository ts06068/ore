# ORE 0.6.0rc2: complete the chat workflow

This release addresses the remaining chat setup, planner recovery and Korean-source validation gaps from 0.6.0rc1. Package versions are `ore-engine 0.6.0rc2`, `@ore/sdk` and web `0.6.0-rc.2`, and `ore-scholarly 0.2.0rc3`. Packages remain local artifacts; this is not a public registry release.

| Requested behavior | Implemented behavior and verification boundary |
|---|---|
| Recover the JACC planner failure | Failed turns retain the conversation and prior plan, with a specific error code and next action. A fresh live JACC June 2024 request with Scopus fallback produced a reviewable plan: six provider turns, 125.20 seconds, 232,124 observed tokens, no error. It stopped at approval; no month-wide download ran. |
| Connect sources from chat | Connection requests create persistent cards without requiring a working model. Credentials and registration details use protected controls. A new source profile follows the connection into the conversation for subsequent planning; Verify checks the requested operation before readiness is claimed. Active collection scope is not silently changed. |
| Agent-assisted API enrollment | A local DOM-capable browser can inspect forms and fill unambiguous saved fields. Ambiguous bindings, login/registration submission, terms and issued-key capture require review in the chat. Actions bind to the current session, control epoch, mission and form; an uncertain effect is not automatically replayed. Ordinary Chrome screenshot desktops still require operator form entry. |
| Organize multiple chats | Purpose folders group conversations at one level; branched conversations retain nested parent/child lineage. This is not arbitrary nested-folder CRUD. Branching preserves public history and settings without copying active runs or credential values. |
| Verify four search sources | Five eligible original-study records per source now have verified main PDFs. The merged corpus has 18 unique main PDFs and 31 supplementary/auxiliary files. Discovery and deterministic continuations were operator-assisted; this is not autonomous chat-to-corpus acceptance. |
| Open the ORE Chrome checkpoint | Missing legacy origin fields can be recovered from an approved persisted checkpoint. The original JACC handoff passed the eligibility preview. Opening a desktop preserves challenge accounting and does not resume collection; this check does not prove publisher access. |
| Show provider usage and collection progress | Codex/Claude official login and explicit OpenAI/Anthropic API connections remain separate. Chat shows provider-reported usage/limits, ORE task budgets, stage progress, verified files and unresolved items. Unsupported limits are unknown; ETA appears only when measured samples support it. |

## Credentials and pending approvals

Named credentials can share a provider connection, but they share quota accounting by default. Independently licensed allocations must be explicitly configured. Throttling and provider reset times cause visible waits, not unlimited collection through account creation or key switching. PubMed's NCBI account supports one active API key; replacing it invalidates the old key. PubMed and Crossref public metadata searches can work without a key.

A saved key is configured, not verified entitlement. Pending applications remain visible and excluded from unavailable operations when the user chooses to continue. Official model login and provider codes stay in protected connection controls, outside ordinary chat messages and model-visible form content. Claude subscription model calls and real provider account issuance have not been newly live verified in this release.

## Actual collection outcome

| Indexed source | Original studies with main PDF | Supplementary/auxiliary file assignments |
|---|---:|---:|
| PubMed | 5 | 8 |
| Scopus | 5 | 15 |
| KISS | 5 | 8 |
| RISS | 5 | 3 |

The Korean samples share two studies and three supplementary files. Deduplication yields **49 unique files: 18 main PDFs and 31 attachments**. Each retained study has indexed membership and exact `cardiac death` evidence in title, abstract or keywords; original research was checked independently against publisher/JATS classification and methods. Metadata hits are not counted as files.

Two PubMed publisher CDN attachment aliases still return 403. Corresponding PMC scientific supplements are included, but their byte-equivalence to those aliases remains unknown. An additional RISS study with an inaccessible current publisher inventory is retained outside the selected five. Bounded sample completion does not establish exhaustive search recall, every publisher's attachment coverage, whole-issue completeness or superiority over standalone Codex.

The merged archive is `.ore/reports/ore-four-source-sample-rc2.zip` (**36,223,354 bytes**, SHA-256 `9b3065176f5cd1fd967bf89af1088ce4dbc590dd5a4291902f7dc0186d3a64c1`). All 49 files passed size/hash and applicable PDF/ZIP checks; Office and MP4 attachments retain their original formats. Its manifest records source membership, provenance, hashes, deduplication and limitations; Korean record file paths resolve to entries in the merged archive. Independent merge verification is recorded in `.ore/rc2-completion/corpus-verification.json`. See [initial PubMed/Scopus evidence](release-0.6-sources.md), [KISS/RISS continuation](release-0.6-rc2-sources.md) and [validation](../VALIDATION.md).

## Validation status

The full Python suite passed 910 tests plus six subtests. The web suite passed 133 tests, and 18 browser UI fixture checks passed. The PDF/Rune subset passed 65 tests plus six subtests, including the live PDF line-wrap identity repair. The SDK passed 16 tests. Fresh core-only and Claude-extra installs, the CLI, sdist rebuild and clean npm SDK install passed. An installed candidate handled chat-led Crossref setup and a real bounded API check without a model or collection job. Full results and running-service promotion are tracked in `VALIDATION.md`; earlier totals are retained as historical evidence rather than reused as current results. Port 8765 now runs rc2; 11 installed UI/API checks passed, including progress endpoints for all 52 preserved jobs and both legacy JACC desktop previews.

The successful JACC plan retains the issue-date window `[2024-06-01, 2024-07-01)`, official-site-first retrieval and Scopus fallback. A separate deliberately constrained run returned an actionable token-budget error; its in-flight overshoot is recorded. Evidence: `.ore/reports/planner-desktop-followup.json`. Neither planner test was approved to collect the full month.
