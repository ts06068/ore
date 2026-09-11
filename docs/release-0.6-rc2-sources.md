# KISS and RISS acceptance in 0.6.0rc2

On September 11, 2026, the bounded Korean-source sample reached **five original research articles per source**, with the exact phrase **cardiac death** in each retained record's title, abstract or keywords. All selected main PDFs and the supplementary files declared by their inspected publisher pages were retrieved and verified.

| Source | Eligible records with verified main PDF | Verified supplementary file assignments |
|---|---:|---:|
| KISS | 5 / 5 | 8 |
| RISS | 5 / 5 | 3 |

Two studies are indexed in both sources. Source membership is counted separately; the downloadable corpus contains **eight unique main PDFs and eight unique supplementary PDFs**, not ten different studies. The three RISS supplementary assignments are files also included in the KISS sample.

This follows the incomplete KISS/RISS attempts in [the initial 0.6 report](release-0.6-sources.md). The earlier failures remain in their original reports.

## What changed

KISS article detail pages returned HTTP 200 during this test and exposed citation metadata and institution download controls. Previous browser 403 responses did not establish that every current public detail request was blocked. The revised context pack tries a small permitted HTML observation before spending a native-agent turn on a large browser page.

RISS returned zero results when literal double quotes surrounded `cardiac death`, but returned 2,130 broad candidates for the unquoted terms. The context pack now separates website query syntax from exact-phrase eligibility: it preserves both observations, filters title/abstract/keywords independently and never labels a broad count as an exact-match or original-study denominator. Thirty initial results and one additional `cardiac death Genoss` query were inspected for this bounded sample.

A RISS title ended at `practi` while the publisher supplied `practice`. The first approximate Crossref suggestions for two KISS records also described different studies. These candidates were rejected; DOI and full title were reconciled against the matching publisher records before acceptance. RISS instructions now explicitly preserve the indexed title while using the verified complete publisher title for PDF identity checks.

One genuine publisher PDF split `infarction` across lines as `infarc-` followed by `tion`, after the first 2,000 extracted characters. ORE initially marked it for review even with the correct DOI and full publisher title. The vault now repairs only a letter-to-letter hyphen at an actual line break, requires an independently matching DOI and checks the complete normalized title. It does not introduce fuzzy matching. Tests cover the real line-wrap pattern, absent/wrong DOI, an ordinary inline hyphen and a different diagnosis with the same DOI. The live retry completed with `doi_guarded_line_hyphen_exact`; original bytes were preserved.

## Execution and evidence boundaries

Discovery was **operator-assisted**. File retrieval and receipts used **operator-authored deterministic ORE workflow v2 stages**, with zero ORE runtime model calls. This verifies the collection tools and the bounded corpus; it does not establish autonomous chat-to-corpus success, exhaustive index coverage or superiority to a standalone agent.

The initial deterministic stage retained four unresolved operations because its manually authored plan used a proposed resource ID after DOI deduplication returned an existing ID. Consolidation follows the canonical DOI/resource and reuses the verified files for both source memberships. The failed stage is retained; it is not relabeled as a successful run. The corrected PDF identity stage completed successfully.

Original-study eligibility was checked against publisher `Original Article` labels, study methods, and JATS `research-article` classification where JATS was available. All eight selected studies have inspected publisher pages. Their observed supplementary file references were reconciled with verified downloads; inline tables are not invented as separate attachments. No claim is made about unobserved future or historical attachments.

An additional RISS-indexed Molecules and Cells article, DOI `10.14348/molcells.2014.2344`, was retrieved and verified from PMC. Its current ScienceDirect page returned 403, so publisher supplementary coverage remains unknown despite no supplementary files in the inspected PMC manifest. That partial record was retained separately and replaced in the requested five with the eligible Genoss study whose publisher inventory was inspected. The additional record is excluded from the sample ZIP and its counts.

No account, credential, challenge or main-service configuration was changed by these runners. All writes were confined to `.ore/v06-korean-completion/` and the sample archive. Initial before/after job and challenge snapshots matched. Concurrent planner validation changed the main job digest during the final retry; the report records that difference rather than claiming the entire main table remained unchanged. Challenge digests matched throughout.

## Artifacts

- Consolidated manifest: `.ore/v06-korean-completion/consolidated-acceptance.json`.
- Archive: `.ore/reports/v06-korean-source-sample.zip`, **6,296,710 bytes**.
- Archive SHA-256: `8719e720a5cb6d7f7a632f37b0b5ed72d4aaf880adae8cceed2e73765d09bb3c`.
- Retained ORE stage receipts: `runtime-report.json`, `identity-continuation.json`, `identity-final.json` in the same acceptance directory.
- Source HTML, publisher manifests, rejected resolution candidates and replay runners are retained privately alongside those reports.

The ZIP includes the 16 verified PDF files, public provenance manifest and README. Every file was read back, checked against its SHA-256 and parsed as a PDF; ZIP CRC validation passed. It contains no credentials, browser state, source-page HTML, database or private access profile.
