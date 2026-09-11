# ore-scholarly

Independent Python 3.12+ metadata adapters and versioned journal contexts for ORE. This package discovers records and artifact candidates; a runtime coordinates network permits, browser sessions, downloads and collection audits.

```python
from ore_scholarly import list_sources, search, resolve, load_rune

page = await search("pubmed", "sepsis", year_from=2024, year_to=2025, limit=20)
next_page = await search("pubmed", "sepsis", year_from=2024, year_to=2025,
                         limit=20, cursor=page["next_cursor"])
candidates = await resolve("pmc", "PMC11333818")
rune = load_rune("hir")
```

`search(source, query, *, year_from=None, year_to=None, journals=None, limit=20, cursor=None, config=None)` returns `records`, `next_cursor`, `total`, `truncated`, `source`, `query`, and provider-specific evidence. `limit` is a single page's maximum, not a request to crawl every result. Stop when `next_cursor is None`; **an empty locally filtered page can still have a continuation**. Reuse source, query, filters and limit exactly. Opaque cursors validate those inputs. `truncated=True` denotes a known hard coverage cap; ordinary resumable pages have `has_more=True` and `truncated=False`. A provider result total is not a complete journal inventory.

`resolve(source, identifier, config=None)` returns `candidates`, `source`, `status`, and discovery evidence. `resolved` means a main PDF candidate was discovered, never that PDF bytes were downloaded or verified. Each candidate carries `id`, `identifier`, `url`, `role`, `version`, license and provenance. Roles include `main_pdf`, `supplement`, `full_text_xml`, `full_text_text`, `landing_page` and unclassified `media`. Source metadata records expose `id`, `url`, `urls`, DOI/PMID/PMCID where available, journal, year, volume, issue, authors, publisher type labels and `eligibility=unclassified`. Database labels do not automatically establish original research.

| Source ID | Implemented operation | Pagination / limits / meaning |
| --- | --- | --- |
| `pubmed` | ESearch + EFetch metadata search | First 10,000 IDs per query; explicit cap and partition warning |
| `crossref` | Metadata search; exact lookup when query is a DOI | Cursor paging; single ISSN uses journal endpoint; other journal filters local |
| `kci` | OAI-PMH `ListRecords`, `oai_kci`, `ARTI` | Local title/abstract substring (`*` = all), publication year and exact journal filters; filtered total unknown; OAI dates are modification dates |
| `scienceon` | Gateway ARTI search with external client ID/access token | XML records and pages; year/journal filters local; protocol still needs live validation |
| `dbpia` | Search API metadata | Numbered pages; Business API/PDF delivery is separate |
| `wos` / `wos_expanded` | Starter v1 / Expanded metadata search | Starter max 50, Expanded max 100 per call; native queries via `query_mode='native'` |
| `scopus` | Scopus Search | Cursor paging; STANDARD max 200, COMPLETE/COMPONENT max 25; native queries optional |
| `unpaywall` | DOI OA-location resolution | Nullable PDF links, original published/accepted/submitted version values; no supplement manifest |
| `pmc` | Current 2026 `pmc-oa-opendata` resolution | Discover actual integer versions; latest or all; bind object URLs to selected article version; parse JATS supplement relationships |
| `google_scholar`, `kiss`, `riss` | Browser/export/link pathways registered for runtime | No API search implementation; calling `search` explicitly raises `operation_unsupported` |

`list_sources(config=None)` separates `capabilities`, `configured`, `protocol_verification` and `live_verified`. The library does not retain a global live-access claim from somebody else's network; historical smoke-check evidence is in the repository validation report. Configuration presence does not establish entitlement.

All credentials are references, with no literal secrets in config or Rune JSON:

```python
config = {
    "client": coordinated_httpx_client,
    "secret_resolver": encrypted_secret_store.get,
    "api_key_ref": "research/scopus/key",
    "insttoken_ref": "research/scopus/insttoken",
}
page = await search("scopus", "sepsis", config=config)
```

Alternatively use `api_key_env`, `email_env`, `client_id_env`, `token_env`, or `insttoken_env`. Defaults are `NCBI_API_KEY`, `NCBI_EMAIL`, `CROSSREF_EMAIL`, `SCIENCEON_CLIENT_ID`, `SCIENCEON_ACCESS_TOKEN`, `DBPIA_API_KEY`, `WOS_API_KEY`, `WOS_EXPANDED_API_KEY`, `SCOPUS_API_KEY`, `SCOPUS_INSTTOKEN`, and `UNPAYWALL_EMAIL`. Only the applicable source reads each variable. `config['sources'][source]` overrides shared settings. `secret_resolver` is a synchronous callable returning a string or `None`. Token acquisition/refresh and account creation are external to this package.

A supplied `httpx.AsyncClient` is used for every HTTP request and is not closed by the adapter. Otherwise `config['transport']` can inject an `httpx.MockTransport`; the package owns its temporary client. Calls do not follow redirects or retry behind the coordinator's back. `ScholarlyError.to_dict()` returns structured failures without provider bodies, request query strings or credentials. Remote XML is parsed with entity expansion disabled; metadata bodies are bounded to 16 MiB. The coordinator still enforces mission origins, source budgets, rate limits and credential scope.

PMC's `version_selection='latest'` chooses the highest discovered integer version; `'all'` enumerates available versions and `PMC123.2` pins one version. This number is not publication status. `is_manuscript=True` is preserved explicitly and normalized as `accepted_manuscript`; a false flag remains `unknown` rather than inventing a published-version guarantee. JATS inspection discovers explicit supplement links; remaining media retains role `media`. MD5 values are preserved for downstream artifact verification. `inspect_jats=False` leaves supplements explicitly uninspected. Binary artifacts are never fetched by a resolver. There is no deprecated FTP fallback.

`list_runes()` and `load_rune(name)` expose `jacc`, `ehj`, `circulation`, `jama-cardiology`, `plos-medicine`, `hir` and `general-web`. These are contextual agent contracts, not claims that selectors or downloads succeeded. Journal families remain outside the main-title scope. The four subscription-journal pilot packs target 2024 and request the first regular issue with an original article and a declared supplement; the exact issue still needs official selection evidence. Other pack windows are adjustable per mission. JAMA Cardiology begins April 2016; earlier years are `not_published`. General web requires the mission's URL, origin scope, selection and output contract. Pack SHA-256 digests bind the exact bundled context.

Run the offline synthetic contracts from the repository root:

```sh
.venv/bin/python -m unittest discover -s tests -p test_scholarly.py -v
```

Official endpoint references are included in `list_sources()`. Account-specific, institutional and full-binary validation remain distinct from these fixture tests.

Release 0.2.0rc1 normalizes explicit provider version labels to `published_version`, `accepted_manuscript`, `submitted_manuscript`, or `unknown`, while keeping `version_raw` and source metadata. Main-PDF collection can require only the published final version; supplementary files have independent roles and do not inherit a manuscript-version assertion.

`ore_scholarly.coverage_profiles.bundled_profiles()` supplies six issue/article profile pairs and an official-publisher JATS profile for the engine's immutable evidence ledger. Trusted publisher counts, exhausted pagination, article identities/types and complete attachment listings establish finite coverage. A references section never proves that supplements do not exist. These templates pass offline structural fixtures; current live-site compatibility, subscription access, earliest-issue selection and twenty-year archive closure are separate validation obligations. Profiles are versioned configuration and should receive new IDs when their extraction contract changes.
