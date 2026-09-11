# Scholarly source validation

Observed on **2026-09-10 UTC**, from the project server unless a row explicitly says official-documentation review. The initial observations below are bounded metadata and public-entry checks. A subsequent actual OA binary acceptance run is recorded in the next section. Neither establishes a complete 20-year inventory or institutional subscription entitlement. The initial metadata/public-entry checks did not use accounts or credentials. Later authorized onboarding and API results are recorded immediately below and supersede the corresponding initial limitations.

## Subsequent authorized onboarding and institutional checks

Elsevier account and API-key issuance completed after the user explicitly accepted the mandatory 2026 API agreement. The actual Scopus STANDARD metadata API returned one record / total one for a native exact-DOI query, with the expected DOI matched. The encrypted key is referenced as `institution/scopus`; no key value appears in this document. No result content was sent to an LLM in this verification. This verifies key usability for that query, not general institution recognition, ScienceDirect full-text delivery, systematic retention or external-model processing rights. Evidence: `.ore/reports/scopus-live-validation.json`.

**WoS Starter was submitted and awaiting provider approval at this check.** Login with an institutional account was verified, a developer profile and application were created, and a subscription request was submitted. The portal reported `Subscription approval is pending`. No key or WoS API query was verified. Evidence remains in the local, untracked reports `.ore/reports/wos-onboarding-status.json` and `.ore/reports/clarivate-institution-subscription-result.json`.

A separate operator-driven browser check confirmed institutional access and returned one matching article for the Core Collection query `DO=(10.4258/hir.2024.30.3.266)`, accession `WOS:001292832000010`, with a publisher full-text link visible. This establishes bounded browser search access; no export file or publisher PDF was fetched, and no API entitlement is inferred. Evidence: `.ore/reports/wos-browser-live-validation.json`.

Four actual Codex workers visited JACC, European Heart Journal, Circulation and JAMA Cardiology. Each encountered a Cloudflare challenge, reached the three-attempt boundary, and handed its browser to the user. All four jobs are `awaiting_user`, with zero artifacts. Entitlement, backfiles and subscription-PDF/supplement retrieval remain unverified. Evidence: `.ore/reports/journal-live-validation.json`; consolidated boundaries: [VALIDATION.md](../VALIDATION.md).

## Actual OA binary acceptance — latest result

**Passed at 2026-09-10T11:02:34.994423+00:00** through the real `Engine` → `ToolRuntime.resolve/resource/download/finish` → `Vault` path, driven by [`scripts/acceptance_oa.py`](../scripts/acceptance_oa.py) without any model call or operator onboarding session. Both selected articles' main PDFs and all four explicit selected-version JATS supplements were downloaded: **6 original artifacts, 3,218,105 bytes**. This supersedes the earlier metadata-only limitation for these two exact OA articles.

The machine-readable result is [`.ore/reports/oa-acceptance.json`](../.ore/reports/oa-acceptance.json). It records DOI/PMCID, selected integer version, each source URL and parent relationship, actual file path/size/SHA-256/MD5, integrity and identity evidence, format inspection, job IDs, audit results and timestamps. Original bytes are retained under the isolated acceptance state's content-addressed vault; the report gives exact paths. No onboarding credential store was used.

| Article | Original filename | Role | Actual bytes | SHA-256 |
| --- | --- | --- | ---: | --- |
| Healthcare Informatics Research | `PMC11333818.1.pdf` | main_pdf | 425,916 | `54777090448405d655d46f7f960ebeaf4368f70679912394a678119e1f9c3b0c` |
| Healthcare Informatics Research | `hir-2024-30-3-266-Supplementary-Fig-S1,2.pdf` | supplement | 491,661 | `5fbf50ea0e46a0336c6c6f95545f5a4ac7b0b6a11aedee9a54bed2a1cca35ca2` |
| Healthcare Informatics Research | `hir-2024-30-3-266-Supplementary-Table-S1,2,3.pdf` | supplement | 213,575 | `4191aebf88e2c5986a67c6d3242b6c92f5fa2d9e08d080b78f227ca74dc9aec6` |
| PLOS Medicine | `PMC11790232.1.pdf` | main_pdf | 1,753,230 | `3a9d2e83276c03b11037af429ad8fc790cc8659b73f454243f6c39a36f1810f0` |
| PLOS Medicine | `pmed.1004493.s001.docx` | supplement | 41,694 | `c17d438e50d6b32d52bf08fc0ecf31782e2d3962b113a2087f38b922f042ce17` |
| PLOS Medicine | `pmed.1004493.s002.docx` | supplement | 292,029 | `3516f6cf57111a662a66682d636e163a3dc5178492b7c4966cea29b259e23351` |

All six artifacts have `status=verified`, `integrity=verified`, matching locally recomputed SHA-256 values and **MD5 equality with the PMC dataset metadata**. The HIR main PDF (`PMC11333818.1`) and PLOS Medicine main PDF (`PMC11790232.1`, DOI `10.1371/journal.pmed.1004493`) both have `identity_evidence.doi_match=true`. HIR's supplements are PDFs; PLOS's two supplements are DOCX files whose ZIP members and CRCs passed inspection. Attachment identity rests on explicit JATS parent relationships and dataset checksums; the test does not require every attachment to repeat the paper DOI.

Both final bounded-job audits returned `complete_within_scope`, with no remaining gaps. This completion applies to the two specified articles and their explicit supplements, **not** all original articles in either issue. Publication status remains the resolver's `unknown` where the PMC metadata does not prove a published-versus-manuscript label; integer version `1` is not relabeled `publishedVersion`.

Reproduce with `.venv/bin/python scripts/acceptance_oa.py`; it creates a fresh isolated acceptance state and overwrites only its named report. It does not call `Engine.start()` or invoke a model/backend turn.

## Initial metadata and contract tests

`ore-scholarly` 0.1.0 was installed editable into `.venv`; the package requires Python 3.12+, httpx and defusedxml. `tests/test_scholarly.py` has **17 passing offline tests** as of this check. Synthetic fixtures cover every implemented metadata adapter, both resolvers, exact DOI lookup, pagination, caps, invalid cursors, credential references, HTTP errors, hostile XML and PMC supplement classification. Fixtures establish parser/contract behavior for those responses, not provider connectivity or account entitlement.

| Source | Operation in this release | Live measurement / remaining boundary |
| --- | --- | --- |
| PubMed | ESearch + EFetch metadata | 10:45:34 UTC batch, 3.22 s: DOI query `10.4258/hir.2024.30.3.266[DOI]`, `limit=1`, total 1; returned PMID `39160785` and matching DOI. No API key/contact email used. |
| Crossref | Cursor search; exact DOI lookup | 10:46:48 UTC: exact DOI endpoint returned `10.4258/hir.2024.30.3.266`, publication year 2024, total 1. First relevance-search smoke check returned an unrelated first hit, which prompted routing DOI queries to the exact endpoint; the exact path was then tested and rechecked live. Broad relevance search must not be interpreted as DOI identity confirmation. |
| PMC | 2026 S3 version/metadata/JATS resolver | 10:45:34 UTC batch, 1.66 s: `PMC11333818` resolved to available/selected version `[1]`. JSON supplied main PDF/XML/text candidates. JATS identified two supplementary PDF references and four other media objects. No PDF or supplementary bytes fetched; no local article files saved. |
| Unpaywall | DOI OA locations, nullable PDF/version fields | **Not run live**: no user-provided contact email was selected for this probe. Fixture-tested. No placeholder email was sent. |
| KCI OAI | ARTI/oai_kci harvest plus local filters | Fixture-tested; no live harvest performed. Query/date/total semantics differ from a search API. |
| ScienceON | ARTI gateway XML search | Fixture-tested; official gateway documentation fetch timed out during review. Client ID, externally issued access token, entitlement and current XML schema require live validation. No token generation or refresh is implemented. |
| DBpia | Search metadata API | Fixture-tested; no API-key request run. Business API and binary delivery are separate. |
| WoS Starter / Expanded | Metadata APIs | Fixture-tested; current official Swagger reviewed. No key or licensed API query submitted. Browser entry observations below do not establish API access. |
| Scopus | Cursor-based metadata Search API | Fixture-tested; no API-key query submitted. Browser entry observations below do not establish API access. |
| Google Scholar / KISS / RISS | Browser, supplied exports and links registered as operations | No API search connector claimed. Browser/export handling belongs to the runtime; no source search or export performed in this probe. |

The PMC observation concerns **ChatGPT Predicts In-Hospital All-Cause Mortality for Sepsis: In-Context Learning with the Korean Sepsis Alliance Database**, Healthcare Informatics Research 30(3), DOI `10.4258/hir.2024.30.3.266`. The JATS relationships name `hir-2024-30-3-266-Supplementary-Fig-S1,2.pdf` and `hir-2024-30-3-266-Supplementary-Table-S1,2,3.pdf`. A discovered filename or URL is not verified binary content. The dataset's integer version is preserved separately from published/accepted manuscript status. The resolver does not classify every `media_urls` object as a supplement. [PMC dataset specification](https://pmc-oa-opendata.s3.amazonaws.com/README.txt)

## Public onboarding entry observations

The following were measured using an unauthenticated httpx client from this server at **2026-09-10 10:48:55 UTC**, with redirects disabled and no existing browser cookies. OAuth query strings, cookies and state tokens were not retained.

| Entry | Measured HTTP response | What this establishes |
| --- | --- | --- |
| `https://developer.clarivate.com/login` | 302 to `https://api.clarivate.com/auth/clarivate/api/portal-api/authorize` | Public developer login begins Clarivate authorization. No login/registration completed. |
| `https://dev.elsevier.com/apikey/manage` | 302 to `https://id.elsevier.com/as/authorization.oauth2` | API key management requires Elsevier identity authorization. No login/key application completed. |
| `https://www.webofscience.com/wos/woscc/basic-search` | 200, HTML title `Web of Science` | Search application shell reachable. This is **not** proof of an entitled collection or successful query. |
| `https://www.scopus.com/home.uri` | 307 to `https://www.scopus.com/pages/home` | Public entry routing reachable. No document search or institution recognition verified here. |

A real institutional browser acceptance check must record an entitled database/collection, an actual search result and the resulting export/link behavior. An HTTP 200 app shell is insufficient. The runtime's remote-browser evidence belongs in its own validation record.

## Access prerequisites from official documents

**Web of Science browser access** can work from an entitled IP range without registration or sign-in. A personal profile/login is a separate feature. Therefore absence of an API key does not prevent testing the institution's browser access. The July 2026 official help page supports this distinction; whether this server's outgoing IP is entitled still needs a browser observation. [WoS sign-in/access documentation](https://webofscience.zendesk.com/hc/en-us/articles/20011617329425-Registering-and-Signing-in-to-the-Web-of-Science)

**WoS Starter API** requires registering an application and obtaining an API key. Its plans have different quotas: free trial 50 requests/day; institutional member 5,000/day; institutional integration 20,000/day. The integration plan is intended for institutional administrative staff. **Expanded** requires a paid license and assigned record/request limits. Existing institution browser access must not be assumed to grant either API plan. These are metadata APIs. [Starter requirements](https://developer.clarivate.com/apis/wos-starter), [Expanded requirements](https://developer.clarivate.com/apis/wos)

**Scopus API onboarding** starts at the Elsevier Developer Portal: an Elsevier user ID is needed before requesting an API key. Full API access depends on affiliation with a subscribing organization. API calls always include an API key; institutional IP authentication is the default. If IP authentication is unsuitable, an institutional token is issued through Elsevier support and must remain server-side, over HTTPS, in `X-ELS-Insttoken`. A key and an institutional token are different credentials. [Scopus onboarding](https://dev.elsevier.com/sc_apis.html), [Developer access scope](https://dev.elsevier.com/), [API authentication](https://dev.elsevier.com/tecdoc_api_authentication.html)

Scopus Search's documented page limits are STANDARD 200 and COMPLETE/COMPONENT 25. The 5,000-result ceiling applies without cursor pagination; this adapter uses cursors and reports stalled/missing continuation explicitly. API quotas still apply. Scopus metadata access is distinct from ScienceDirect full-text delivery. [Official quota and page-limit table](https://dev.elsevier.com/api_key_settings.html)

In ORE, enter a secret through the encrypted store or a named environment reference, then pass `api_key_ref`/`email_ref`/`insttoken_ref` with `secret_resolver`. Do not paste actual keys into a Rune, README, command line, repository file or model prompt. The `configured` registry flag reports only reference presence; `live_verified` is not inferred from that flag.

## Rune scope and open pilot separation

Seven bundled Rune contexts cover the **main titles** JACC, European Heart Journal, Circulation, JAMA Cardiology, PLOS Medicine and Healthcare Informatics Research, plus general web content. They do not cover every journal in a publisher family. JAMA Cardiology's first issue is April 2016, so earlier requested years are `not_published`, not missing downloads. Its source is the [first issue](https://jamanetwork.com/journals/jamacardiology/issue/1/1). PLOS Medicine's official [journal archive](https://journals.plos.org/plosmedicine/volume) was inspected and confirms the bundled archive entry.

The historical HIR PMC metadata check was followed by the successful two-article OA binary acceptance above. The four subscription-journal browser checks subsequently ran and ended in human handoff as recorded above. Institution recognition, complete TOC reconciliation and subscription-file acceptance remain unresolved. Bundled contexts and unit tests must not be presented as successful collection of those journals.
