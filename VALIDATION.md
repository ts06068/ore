# ORE 0.1.0 validation record

Measured on **2026-09-10** in this workspace. This is a working first release with bounded acceptance evidence, not a completed twenty-year journal corpus. Python and npm distributions are built locally; no registry publication is claimed. Raw acceptance state and reports remain local under `.ore/` and are intentionally excluded from distributable packages.

## Implemented and exercised

| Area | Actual result | Evidence / reproduction |
| --- | --- | --- |
| Python regression suite | **102 passed, 2 subtests passed**, 22.11 s. Includes real local Chromium fixtures and fake-provider contract tests; this count does not mean 102 remote service tests. | `uv run pytest -q -m 'not live'`; `tests/` |
| Subscription Codex → browser → artifact | Actual ChatGPT-authenticated local Codex, Astra/high; four model decisions; exact requested paragraph saved and audit completed in **23.11 s**. No API key or local model GPU used. | `scripts/acceptance_codex.py`; `.ore/reports/codex-acceptance.json` |
| Independent remote Codex workers | **Passed in 24.96 s**: two distinct workers/Codex threads, eight actual Astra/high turns, two exact excerpts with verified hashes and complete audits. Recorded execution overlap 19.93 s. Same-machine workers over authenticated HTTP; separate physical hosts/VMs were not tested. | `scripts/acceptance_remote.py`; `.ore/reports/remote-codex-acceptance.json`; journal section below |
| Empirical model routing | **5 paired extraction fixtures**, Astra/high versus Terra/low; all 10 arms passed exact expected output and audit, no failures. Final run 215.46 s. Separate actual automatic-routing job selected Terra/low on all four turns and completed in 17.56 s. | [Evaluation protocol and hashes](docs/evaluation.md); `scripts/calibrate_routing.py`; `scripts/acceptance_routing.py` |
| Main PDFs and supplements | Two exact OA articles: **6 original files, 3,218,105 bytes**, saved through Engine/ToolRuntime/Vault with SHA-256 and PMC MD5 equality. Main-PDF DOI identities matched; DOCX CRCs passed. This acceptance drove runtime tools directly and made **zero model calls**. | [File-level results](docs/source-validation.md); `scripts/acceptance_oa.py`; `.ore/reports/oa-acceptance.json` |
| Scopus API | User-authorized Elsevier key issued; one actual native exact-DOI STANDARD search returned one matching record / total 1. No returned content sent to an LLM in this probe. | `scripts/verify_scopus.py`; `.ore/reports/scopus-live-validation.json` |
| Web of Science browser | Yonsei University label observed; one exact DOI query returned the expected article from Core Collection (`WOS:001292832000010`), with publisher full-text link visible. Operator-driven ORE browser, not an API test; no export/PDF downloaded in this probe. | `.ore/reports/wos-browser-live-validation.json` |
| UI and TypeScript SDK | Seven UI contract tests and seven SDK tests passed. Real built-image browser smoke passed seven steps including authentication/session restore, draft → run → pause, Rune editing, browser frames/input and mobile layout; no page errors, zero model calls. | `web/test-results/summary.json`; `deploy/docker-smoke-result.json` |
| PostgreSQL and containers | Compose smoke preserved the created job after coordinator restart. Final image runs as UID 10001, health endpoint and packaged UI pass; no Codex credential mount or Codex binary in coordinator. | `deploy/smoke-result.json`; [Deployment instructions](deploy/README.md) |
| Local release installation | Both Python wheels/source archives and npm SDK tarball built. Clean Python environment install/import/CLI/static checks, wheel rebuilt from source archive, and clean npm tarball install passed. | `scripts/build_release.py`; `dist/release-manifest.json`; `dist/SHA256SUMS` |

The actual Codex backend uses **schema-constrained next-action turns** and ORE's typed tool executor. Native dynamic-tool dispatch required an unavailable code-mode host; widening that execution boundary was rejected by automatic approval review. The tested alternative leaves Codex shell, native browser/computer tools, external MCP/plugins and code-mode host disabled.

Final calibration runtime digest is `67e62ddf326fb26d27d8bb4f296b45cab2feb019829b0ecb17fc74554a9927e0`. The five-pair measured totals were Astra 20 decisions / 249,412 tokens / 118.29 s and Terra 20 decisions / 227,068 tokens / 93.12 s. These are controlled local paragraph extraction results, **not** a monetary saving, generalized model equivalence or architecture-superiority experiment. No lower-tier route is justified merely by setting an unverified boolean. See the evaluation document for the validated report and separate automatic-selection proof.

## Accounts and institutional sources

The Elsevier key is stored only through encrypted secret reference `institution/scopus`; the institution onboarding profile points to that reference. The user explicitly accepted the mandatory API agreement before issuance. Optional additional TDM terms were not selected. A metadata API key does not authorize arbitrary full-text retention, remote-model processing or bulk subscription-PDF downloads; those were not established by the Scopus query.

**WoS Starter application submitted; awaiting Clarivate approval.** The user supplied credentials for an existing verified Yonsei institutional account. Login was confirmed against its institutional email privately, its developer-portal profile was registered, and ORE application `ore-yonsei-20260910` was created with the supplied Yonsei University affiliation. The **Free Institutional Member Plan** request was submitted successfully at 12:03:58 UTC; the portal explicitly reports `Subscription approval is pending`. Its displayed quota is 5 requests/second and 5,000/day after approval. No active WoS key or API query is yet verified. Evidence: `.ore/reports/wos-onboarding-status.json` and `.ore/reports/clarivate-institution-subscription-result.json`.

The earlier Gmail application was left unsubscribed; no account merge or deletion was performed. The earlier attempt to add the institutional email to that account was superseded by successful login to the existing institution account, so email-addition verification is no longer a prerequisite. A separate browser check successfully queried Core Collection under the observed Yonsei University institution label; that does not establish API subscription approval.

| Requested journal | Actual entry result with real Codex worker | Artifact result |
| --- | --- | --- |
| JACC | Cloudflare challenge; three bounded attempts; same browser handed to user | 0 files; institutional full text unverified |
| European Heart Journal | Cloudflare challenge; three bounded attempts; same browser handed to user | 0 files; institutional full text unverified |
| Circulation | Cloudflare challenge; three bounded attempts; same browser handed to user | 0 files; institutional full text unverified |
| JAMA Cardiology | Cloudflare challenge; three bounded attempts; same browser handed to user | 0 files; institutional full text unverified |

The four jobs have `awaiting_user` status and preserved browser handoff identifiers in `.ore/reports/journal-live-validation.json`. This verifies that real workers stop and hand over; it does **not** verify challenge success, IP entitlement, licensed backfiles, issue enumeration or any subscription main/supplement download. Initial failed probes and pre-fix task rows are retained as historical state; the package contains the subsequently tested lease/settlement fixes. The operator browser process is retained during account onboarding so its current human-controlled pages are not destroyed by a server restart.

## What remains unverified or deliberately bounded

- The two successful OA articles are HIR DOI `10.4258/hir.2024.30.3.266` and PLOS Medicine DOI `10.1371/journal.pmed.1004493`. Completion covers their explicit selected-version JATS supplements only. PMC integer version 1 remains distinct from publication status; unknown publication status is retained.
- No twenty-year retrieval or complete issue-by-issue official-TOC reconciliation has run. `global_recall` remains `unknown`; indexed databases can disagree. JAMA Cardiology's pre-2016 years are not published, not missing files.
- PubMed, Crossref and PMC have bounded live checks. Unpaywall, KCI, ScienceON, DBpia, WoS and other licensed paths have fixture/documentation coverage as specified in [source validation](docs/source-validation.md), not blanket live success. Google Scholar/KISS/RISS have browser/export routes rather than claimed official search APIs.
- OpenAI API, Anthropic API and separately served local models are optional implemented backends without live authenticated validation here. There is no automatic paid fallback. Codex quota availability may change.
- S3 replication is fake-client tested, including read-back SHA checks; no live AWS/S3-compatible deployment was exercised. OCR is optional and has not been validated on a real scanned corpus.
- This release has parallel agent workers and one coordinator/browser service. Multi-host browser distribution, multiuser SaaS and Windows operation are not established. `max_seconds` applies per task attempt; persisted turn/token/byte budgets have separate scope.
- Persistent session storage restores browser cookies/storage after process restart, not the previous live DOM. URL/DNS checks are not a complete network isolation or DNS-rebinding defense.
- No anonymity, suppression of provider/institution logs, universal challenge success, unrestricted collection rights or globally comprehensive search is promised.
- CI definitions are included; remote hosted CI was not run. Distributions were not uploaded to PyPI or npm.

## Reproduction

Use the installation instructions in [README.md](README.md). Ordinary tests run against fixtures. The acceptance and calibration scripts explicitly consume real Codex subscription usage or make small network requests. Preserve the report and its referenced state together when auditing actual artifact bytes; the reports alone are not the binary corpus.

```sh
uv run pytest -q -m 'not live'
uv run python scripts/acceptance_codex.py
uv run python scripts/acceptance_remote.py
uv run python scripts/acceptance_oa.py
uv run python scripts/build_release.py
```

Account-registration scripts are one-time local onboarding helpers, not repeatable tests; rerunning them could create duplicate applications or keys. Credential values must remain outside source, public reports and package archives.
