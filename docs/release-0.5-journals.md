# ORE 0.5 native journal acceptance

This bounded live run did **not** pass exhaustive issue collection. All four official issue tables of contents returned HTTP 403, leaving the original-research and supplementary-file denominators unverified. The native workflow downloaded one valid EHJ PDF and no verified supplementary files. This is partial retrieval evidence, not evidence that ORE outperforms standalone Codex on journal collection.

The run used the production native workflow with the concurrent Codex startup fix, before the subsequent recoverable-transport-error and paused-budget-clock corrections. Those later corrections were not rerun against these journals; the original evidence remains unchanged.

## Execution and results

Execution ran from `2026-09-10T23:39:15.876983Z` to `2026-09-10T23:42:08.191964Z` (172.31 seconds). Four isolated jobs used `gpt-6-astra`, `high`, native tool dispatch, and fixed model selection. Each job had a 300-second and 350,000-total-token allowance. The observed token totals include cached input tokens and all internal completions within a provider turn.

| Journal issue, 2024 | Workflow outcome | Verified main PDFs | Verified supplements | Observed tokens | Token overshoot |
| --- | --- | ---: | ---: | ---: | ---: |
| JACC 83(1) | `needs_replan` | 0 | 0 | 95,007 | 0 |
| European Heart Journal 45(1) | `paused_budget` | 1 | 0 | 358,794 | 8,794 |
| Circulation 149(1) | `paused_budget` | 0 | 0 | 397,849 | 47,849 |
| JAMA Cardiology 9(1) | `paused_budget` | 0 | 0 | 387,214 | 37,214 |

There were 100 dispatched registry tool calls, 79 recorded HTTP requests (72 HTTP 200, five 301, one 302, one 429), and four provider turns. Total observed usage was 1,238,864 tokens. A provider turn includes multiple internal model completions, so four turns does not mean four model inference steps. Reported token overshoot is retained rather than hidden.

JACC encountered an `httpx.InvalidURL` exception while fetching an existing PMC article URL; the error concerned a newline in a subsequently handled URL. The exception ended that worker instead of returning a recoverable tool observation. EHJ, Circulation, and JAMA reached their token allowances. At the time of this run, their public budget snapshots still showed `paused=false` and elapsed time continued after individual workers stopped. The stored snapshots describe that observed behavior and have not been rewritten to reflect later fixes.

## Artifact verification

The verified file is *The Vienna Prediction Model for identifying patients at low risk of recurrent venous thromboembolism: a prospective cohort study*, DOI `10.1093/eurheartj/ehad618`. Its first page identifies **European Heart Journal (2024) 45, 45–53** and **CLINICAL RESEARCH**. Independent local `pypdf.PdfReader` inspection read all nine page objects and extracted the matching first-page title and DOI.

- Filename: `ehad618.pdf`; size: **722,770 bytes**.
- SHA-256: `3fd1e8490f58fecb4849a74a23f53698a0e3c4e70ff1bd6073d3c7dca3c33a2a`.
- Retrieval source: `pmc-oa-opendata.s3.amazonaws.com`, object `PMC10757868.1/PMC10757868.1.pdf`.
- Stored object: `.ore/journal-v05/native-20260910T233914Z-7dacd3/vault/objects/3f/3fd1e8490f58fecb4849a74a23f53698a0e3c4e70ff1bd6073d3c7dca3c33a2a`.
- The workflow's version metadata remained `unknown`; independent PDF inspection does not silently revise that metadata or establish the whole issue inventory.

A second EHJ download returned 1,817 bytes of HTML and was retained as an **invalid** artifact, excluded from the valid PDF count. Its SHA-256 is `1138a3456f3f5512a8ccde1ade9775843566b84246153ceaf7d8f37bb2cf0b8c`. A further source returned HTTP 429. No supplementary completeness claim can be made from these results.

## Access, isolation, and evidence

The official TOC preflight preceded API/OA-first native retrieval. PubMed provided candidate records, and PMC/Unpaywall provided retrieval locations. PubMed article types were not treated as an independently verified publisher inventory. Historical DOI pointers were allowed as access hints only; no previously downloaded files were reused. WoS and Scopus were explicitly excluded from admission while unavailable/pending. Existing institutional secret references were resolved privately.

No browser calls were made, and no exhausted challenge episode was reset or copied into a clean profile. The existing main store contained **47 jobs and seven challenge documents** at the start of this run; their canonical digests were identical afterward. New workflow state and artifacts are isolated under `.ore/journal-v05/native-20260910T233914Z-7dacd3`.

Evidence paths are local generated artifacts, not package contents:

| Evidence | Path | SHA-256 |
| --- | --- | --- |
| Final observed journal report, including redacted calls and artifact checks | `.ore/journal-v05/native-acceptance.json` | `2e7f2c5a8b55674b3a4064800b7565b99727fc55f7e3d9cd87b4ad00a22048f9` |
| Official access preflight | `.ore/journal-v05/official-access.json` | `8c56b92aa53e13951278c9ff473462fa725f00e3e81a02dc0b3fb849a354ce46` |
| Preserved initial concurrent-start failure, before the startup fix | `.ore/journal-v05/native-acceptance-startup-race-pilot.json` | `9ced9553faf406f3f1146cd492f5cbc88cf0a478c22fa01ba77012178b4a5615` |
| Native adapter/public-stream regression tests, 42 passed | `.ore/reports/native-adapter-v05-tests.xml` | `07fcab833cf25f63b79bf337753d1cc7228a24316c49b22be12d5d6b29d50b97` |

The regression command was `python -m pytest -q tests/test_agent_sessions.py tests/test_public_stream.py --junitxml=.ore/reports/native-adapter-v05-tests.xml` (42 passed in 2.63 seconds). These tests validate adapter contracts; they do not substitute for journal completeness or a frozen comparison benchmark.
