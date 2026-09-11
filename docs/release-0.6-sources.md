# Four-source acceptance in 0.6

The real bounded test requested five original research articles from each of KISS, RISS, Scopus and PubMed, with the exact phrase **cardiac death** in the title, abstract or keywords, without a year limit. Collection used the existing permitted source configuration and API/OA-first paths.

| Source | Independently eligible original studies with main PDF | Verified supplementary files | Result |
|---|---:|---:|---|
| PubMed | 5 / 5 | 8 | Five main PDFs plus four research supplements and four reporting checklist/COI files. Two publisher CDN aliases returned 403; corresponding PMC scientific supplements were retrieved, but CDN byte-equivalence remains unverified. |
| Scopus | 5 / 5 | 15 | Five indexed studies independently verified through publisher/PMC JATS; all 15 supplementary files in their observed PMC manifests retrieved. This is bounded manifest coverage, not a guarantee about uninspected publisher pages. |
| KISS | 0 / 5 | 0 | Browser returned 403. Public HTTP fetch returned the search interface with a dash for result count and no verifiable article records. This does not establish zero matches. |
| RISS | 0 / 5 | 0 | Homepage loaded and the exact phrase was entered; the native token limit was reached before search submission. The result count remains unknown. |

The corpus contains **10 main PDFs and 23 supplementary/auxiliary files**: 19 research supplements and four checklists/COI disclosures. All 33 files were read from disk and their SHA-256 values verified. The 32 PMC-hosted main/supplement files also match the observed provider MD5 values; the externally hosted DOCX passes ZIP CRC validation. PDFs parse as PDFs; ZIP-based office files pass CRC checks. Original-study eligibility is supported by indexed record membership, JATS `article-type="research-article"`, Methods section evidence and the literal phrase in each of the ten abstracts.

The supplementary references were reconciled separately from data-availability boilerplate. The Frontiers PubMed paper contains an article/Supplementary Material data-availability statement, but its inspected JATS declares no supplementary file href. That statement is not counted as a missing file or proof that no supplement exists elsewhere. The two unresolved AME CDN URLs are actual attachment references; their separately declared PMC supplementary PDFs contain scientific tables/figures for the matching parent DOI, but inaccessible CDN aliases were not declared byte-identical.

## What ran

Initial collection used production v2 **native Codex workflows**. The first KISS/RISS attempts failed before site access because the standalone runner omitted the existing Playwright executable environment. Those failures are retained. Their subsequent browser attempts used the installed Chromium with allowances below the unspent first-attempt limits.

Scopus's first preformatted query was incorrectly wrapped as a literal string by the then-current adapter and returned zero. The agent's plain phrase query succeeded: HTTP 200, 56,525 reported records and ten returned indexed records. That total is not an original-study denominator. Four DOI-to-PMC lookups succeeded before the native token cap; no Scopus PDF had yet been downloaded. The query-mode bug was fixed afterward and the original evidence remains unchanged.

PubMed's native run downloaded five verified main PDFs. Subsequent **operator-authored deterministic tool DAGs**, using already observed indexed records and declared PMC/publisher links, downloaded supplementary files and completed Scopus retrieval. The deterministic stages made **zero model calls**. These continuations demonstrate the tool execution path and receipt/verification behavior; they are not counted as automatic-agent-only success.

The first deterministic continuation retrieved seven PMC supplementary files plus five XML documents in 41.2 seconds. The next stages completed in 128.0 seconds, including five Scopus XML documents, five Scopus main PDFs, 15 Scopus supplementary files and one external PubMed DOCX. All previously observed PMC checksums used by the first continuation matched.

The native attempts reported 577,912 cumulative tokens, including 98,029 tokens of observed in-flight overshoot across the per-attempt caps. Limits were not raised on exhausted runs, and no additional model continuation was launched. Browser observations caused particularly large usage jumps; this is a remaining cost-control limitation. The initial `needs_replan` elapsed-clock defect was also observed and retained in the first report, then fixed in the runtime separately.

## Artifacts

- Machine-readable consolidated report: `.ore/v06-sources/consolidated-acceptance.json`.
- Shareable corpus: `.ore/reports/v06-source-sample.zip`, **29,928,133 bytes**.
- ZIP SHA-256: `2cdeb4b9c3e279dbf21d65b016e6644f19355c844e5bdfec089106bbd13f286e`.
- ZIP contents: verified main/supplement files, public `manifest.json` with hashes/source URLs/eligibility evidence and a short README. No database, credentials, access profiles, browser sessions or XML source files are included.
- Retained stage reports: `attempt-1.json`, `native-browser-retry.json`, `deterministic-supplements.json`, `zero-model-continuation.json` under `.ore/v06-sources/`.
- Runners: `/tmp/ore-v06-sources.py`, `/tmp/ore-v06-sources-retry.py`, `/tmp/ore-v06-supplements.py`, `/tmp/ore-v06-continue.py`; each writes isolated state.

The consolidated report verifies that the existing **48 main jobs and seven challenge documents** were unchanged. No account enrollment, API-key issuance, challenge reset or main service restart was used by this test. All-four-source completion and universal superiority over Codex are **not established**.
