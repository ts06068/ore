# ORE 0.2.0rc1 release guide

ORE 0.2 adds evidence-backed finite journal collection, source-operation admission, durable human handoffs, and separate execution/validation services. It remains a release candidate. A real Astra/high run has completed a controlled whole-issue fixture through the normal Engine. **A complete issue from JACC, European Heart Journal, Circulation, or JAMA Cardiology, and a twenty-year journal corpus, are not established by that result.**

The Python engine and scholarly extension use version `0.2.0rc1`. The npm package is a client SDK for the running service; it is not a separate scraping engine. Final distribution/build checks and current institutional-source results are recorded separately by the release operator.

## Scope and completion contract

New missions use `ore.mission/v2`. Journal Rune packs default to `completeness: systematic`; explicitly bounded general-web jobs retain their narrower contract. A finite journal mission supplies the selected issue URLs in `scope.issue_urls` or the top-level `issue_urls`. Engine creation seals this explicit target set. The model then discovers the identities and files inside that set from captured source evidence; the user does not supply a denominator or expected file hashes to the model.

The four subscription-journal packs request the first regular 2024 issue containing original research and at least one declared supplement, restricted to the main journal rather than its journal family. This selection still needs official archive evidence. The current ledger can establish coverage of explicitly selected issues; it **does not implement complete-archive sealing or independently prove that a selected issue is the earliest qualifying issue**. JAMA Cardiology did not exist throughout a twenty-year retrospective window. Neither an empty pre-launch year nor an inaccessible issue may be silently treated as a successfully collected issue.

For systematic collection, the coordinator requires:

1. A locked finite collection target.
2. A sealed issue identity manifest parsed from complete executor-captured HTML using a trusted, versioned extraction profile. Pagination, publisher counts when present, pending content and unaccounted article links are checked.
3. A sealed article manifest tied to those identities. Publisher article labels establish inclusion/exclusion; unrecognized or conflicting labels remain unresolved. Broad section labels such as JAMA's `Research` do not prove original research. Review-like titles need a reviewed eligibility rule.
4. A complete authoritative attachment listing, including an explicit empty-manifest basis when no supplement exists. Reaching a references section, failing a search, or receiving a challenge page cannot establish absence.
5. Every required file bound to its sealed candidate, role and article, with verified actual bytes and a preserved SHA-256. A resource record or a stored `status=verified` flag by itself does not satisfy the requirement.

`state`, `fetch` and browser observations expose usable snapshot/profile identifiers. `seal_issue` and `seal_article` accept identifiers of captured evidence and trusted profiles; they do not accept model-authored source bodies or declared counts. Sealed article results expose the `candidate_id` and `requirement_id` needed for downloads. The corresponding resource must use the sealed `article_key` as its canonical identity.

The coordinator stores immutable content-addressed snapshots and manifest digests bound to the job, revision, inventory generation and Rune digest. The actual extraction profile is preserved with the manifest. Concurrent identical captures are idempotent; conflicting sealed writes require a new revision. HTML captures remove form values, scripts and token metadata, record the transformations and original/sanitized hashes, and reject password forms as scholarly evidence. Collected original files remain separate from sanitized HTML evidence.

## Versions, origins and retrieval order

Provider version labels normalize to `published_version`, `accepted_manuscript`, `submitted_manuscript`, or `unknown`. Raw labels and provider evidence remain available. Unpaywall's accepted manuscript is not interchangeable with a publisher's final PDF. A PMC dataset version number is not publication status; `is_manuscript=False` alone remains insufficient to assert a published final version.

The requested final-version restriction applies to the main PDF. A supplement can have `version: unknown` and still satisfy its own requirement when its parent relationship, declared role, source and actual bytes are established. Supplements are not required to repeat the article DOI inside their contents.

The evidence priority remains **journal → publisher → PMC**. Official issue and article manifests establish the collection denominator and attachment obligations in both retrieval modes. A database search total, OA location or model declaration cannot replace those manifests.

With the default `official_first` policy, a lower-tier download requires executor-recorded failure/unavailability at the higher tiers. With `api_open_access_first`, that higher-tier failure prerequisite is waived so an eligible publisher/PMC copy can be downloaded first. This exception changes transport order only: candidate authority, identity, requested main-PDF version, supplement equivalence, immutable evidence and final audit requirements remain enforced. A trusted profile can state that the journal and publisher use the same delivery route; under `official_first`, failure of JACC's journal route does not automatically establish failure of a distinct ScienceDirect route.

Candidate URL, resource identity and role are checked before downloading. An executor-recorded redirect chain can preserve a candidate's identity through a publisher CDN, while network policy still applies at every hop. Cost/latency rankings remain estimates, labeled `uncalibrated_defaults` when no profile estimates are supplied; they cannot establish access rights or override content verification. PMC/publisher supplement equivalence is not inferred from a similar filename; an unmatched alternative remains a gap.

### Retrieval policy

`retrieval_policy` controls retrieval paths independently of model `routing`:

| Field | Behavior |
| --- | --- |
| `mode: official_first` | Default. Apply journal → publisher → PMC delivery priority and require recorded higher-tier unavailability before a lower-tier download. |
| `mode: api_open_access_first` | Prefer admitted institution/public metadata APIs and OA resolvers before optional browser fallback. Waive only the higher-tier download-failure prerequisite; require official coverage evidence and eligible file candidates. |
| `browser_fallback: true` | Default. Automatic browser tools remain available subject to source, profile and network restrictions. |
| `browser_fallback: false` | Disable automatic browser tools in local and remote execution. Leave inaccessible official inventories and missing files as unresolved gaps. |

At mission creation, an omitted `retrieval_policy` inherits the selected access profile's policy. If both omit it, the defaults are `official_first` and `browser_fallback: true`. The effective policy is normalized into the saved mission. A profile that disables browser fallback also restricts missions requesting it; changing a profile does not silently rewrite a saved mission's retrieval mode. Existing source-operation exclusions, readiness checks, credentials, URL/DNS restrictions and publisher entitlements still apply.

This example selects PubMed/Crossref metadata search and PMC/Unpaywall resolution with automatic browser fallback disabled. Use an existing access profile ID; for systematic journal collection, also supply the journal Rune and actual `scope.issue_urls`.

```yaml
goal: Retrieve original articles and all declared supplements within the selected issues.
access_profile: institution-onboarding
sources: [pubmed, crossref]
artifact_roles: [main_pdf, supplement]
retrieval_policy:
  mode: api_open_access_first
  browser_fallback: false
source_policy:
  allow:
    search: [pubmed, crossref]
    resolve: [pmc, unpaywall]
```

The execution plan reports admitted `available` routes and `skipped` routes with readiness reasons. A selected operation in `approval_pending`, `unconfigured` or an excluded state is skipped without a request; optional alternatives remain usable. `unknown` means eligible for an access probe, not verified access. Unpaywall requires a configured contact email. A required but unavailable source remains an audit obligation even when another source succeeds.

The read-only preflight requires an explicit state directory and profile. `--dry-run` produces a network-free plan; without it, the script performs bounded metadata/resolver probes. It does not start an Engine/model/browser or download main/supplement bodies, and its report cannot prove complete collection.

```sh
.venv/bin/python scripts/acceptance_api_fallback.py --state-dir .ore --profile institution-onboarding --dry-run
```

## Status, missing sources and progress

| Result/state | Meaning |
| --- | --- |
| `completed` with audit `complete_within_scope` | Every requirement of the explicitly sealed finite scope passed. Global search recall remains unknown. |
| `finished_incomplete` | Available task execution ended with unresolved evidence or required-file gaps. |
| `awaiting_user`, `awaiting_auth`, `awaiting_source` | A durable intervention or source dependency prevents further automatic progress. |
| `paused_budget` | The configured execution budget stopped the work. |
| `legacy_bounded` audit | Historical evidence retains its earlier bounded interpretation; it has not been promoted to systematic completeness. |

An unavailable file is reported separately from an uninspected file, and neither is counted as collected. A required source operation cannot disappear from the final audit simply because another route was available. Known user-reported inventory gaps also remain gaps.

`inventory_verified` and `coverage_denominator` come from sealed evidence. An unknown denominator has no reliable completion percentage. ETA is conditional on a closed inventory and sufficient observed durations; a handoff or unknown inventory must not be rendered as an invented remaining time.

## Source operations and access readiness

The source catalog separates implemented capability, credential configuration and observed readiness. Readiness belongs to an **access profile × source × operation**, not to a product name. `ready` requires timestamped evidence, and expired evidence becomes unknown. A configured API reference does not prove that a key was issued, a plan was approved, the server has institutional entitlement, or PDFs are accessible.

| Source | Implemented route and boundary |
| --- | --- |
| PubMed | Metadata search; result caps/pagination do not establish an original-article or full-text inventory. |
| Crossref | Deposited metadata search and links; no general PDF/supplement delivery guarantee. |
| KCI | OAI-PMH metadata harvesting with local query filtering; OAI dates are modification dates. |
| ScienceON | Metadata gateway with externally obtained credentials/token; entitlement and current provider schema still require live validation. |
| DBpia | Metadata search API; PDF access is a separate route/entitlement. |
| WoS Starter / Expanded | Distinct metadata API plans/keys; institutional browser access is a separate capability. |
| Scopus | Metadata search API; API key, institutional network/token and publisher full text are separate access conditions. |
| Unpaywall | DOI-based OA locations and explicit version labels; PDF URLs may be absent and supplements are not inventoried. |
| PMC | Current Article Datasets resolver; available main/metadata/media objects and explicit JATS supplement relationships remain distinct. |
| Google Scholar, KISS, RISS | Browser, official-export and link workflows; no invented metadata-search API support. |

There is no dedicated institutional subscription full-text API adapter in this release. Existing institutional network or approved proxy access can be used by permitted HTTP/API routes. WoS and Scopus API results are metadata, not PDF delivery or proof of publisher backfile/supplement entitlement. Unpaywall and PMC discovery also do not guarantee a published-final main PDF plus every official supplement.

Mission `source_policy` contains operation-specific `allow`, `exclude`, and optional `required` source lists. Operations are `search`, `resolve`, `browser`, `download`, and `import`. An explicit empty allowlist denies that operation; exclusions win over wildcards. Profile restrictions intersect mission permissions. A source disabled in the profile is excluded from every operation.

A pending WoS API application blocks `search: wos` without retrying the pending API. Institutional `browser: wos` is a separate operation and is available only when source/profile policy permits it and browser fallback is enabled. Optional sources can fail with structured recoverable observations and permit fallback. Explicitly required source operations need successful executor evidence in the current revision/generation; old readiness or a prior revision's success is insufficient. API endpoints retain their semantic search/resolve policy even when reached through a generic fetch or browser action.

Credentials remain encrypted references or approved environment references. They are not mission text, tool arguments, package constants, or material for the model. The Connections/handoff workflow separates setup requests and observed access checks. Source-specific signup or a CAPTCHA can require the user; no automatic approval, entitlement, challenge bypass or anonymity is implied.

## Confirmed whole-issue fixture: actual model execution

On **2026-09-10**, [acceptance_scholarly_fixture.py](../scripts/acceptance_scholarly_fixture.py) ran a real fixed **Astra/high** agent using the normal Engine/action loop. It installed only the trusted fixture extraction profiles before execution. No scripted model responses or manually driven collection tools were used.

The source was a controlled local HTTP issue containing an original article and an editorial. Its independently computed expected identity/file manifest remained in the runner process and was absent from the mission, Rune, model prompt and served routes. The model discovered the scope from the source pages.

| Measured result | Value |
| --- | --- |
| Actual model decisions | 14, all Astra/high |
| Elapsed execution and audit | 91.922 seconds |
| Issue inventory | 2 identities: 1 included original article, 1 excluded editorial |
| Original files | 1 main PDF, 1 CSV supplement, 1 ZIP supplement |
| Actual file bytes | 986 + 60 + 184 = **1,230 bytes** |
| Independent verification | Exact URL/role/byte count/SHA-256 match for all three files; main PDF title and DOI verified |
| Final Engine/audit | `completed` / `complete_within_scope`, zero gaps |
| Runtime changes during the measured run | None in the recorded runtime fingerprint |

The model's observed sequence was `state → fetch → seal_issue → fetch → seal_article → resource → download × 3 → fetch → seal_article → resource → state → finish`. It also inspected and registered the excluded editorial.

The preserved report is [whole-issue-fixture.json](../.ore/acceptance-scholarly-v02/20260910T131246Z-134a82/reports/whole-issue-fixture.json), SHA-256 `88c6de91ba88bf1346c2e1a9526e0b8be040c7a3d0b00efe0d515b12695ff2c3`. It includes exact source hashes, hidden expectations, sealed profiles/manifests, actual artifact checks, events and observed usage. This workspace report is empirical evidence, not a file guaranteed to ship in a wheel.

The result demonstrates the complete controlled workflow. It does not validate a publisher's current DOM, institutional access, earliest-issue selection, a real full issue, complete archives, or twenty-year recall. Subsequent conservative regression fixes preserve migrated legacy tags and flag broad/review-like article types; their dedicated offline tests pass, but the historical report remains evidence of the precisely recorded run rather than a claim of a new rerun.

To repeat the explicit real-model fixture from this workspace:

```sh
.venv/bin/python scripts/acceptance_scholarly_fixture.py
```

The script uses fresh state under `.ore/acceptance-scholarly-v02/`, an isolated local fixture server and no onboarding credentials. Its browser paths reflect this workspace; another deployment must supply its installed Chromium environment. It calls the configured Codex backend and consumes actual model usage.

## Template and archive limitations

Bundled issue/article profiles cover JACC, EHJ, Circulation, JAMA Cardiology, HIR and PLOS Medicine, plus an official-publisher JATS profile. All six HTML profile pairs have passed structural fixtures, including three distinct supplementary links. This is not live-template verification. Unrecognized pages, pending sections, DOI/type conflicts, missing attachment closure, unsupported versions and hash changes fail closed. Publisher template changes require reviewed profile updates with new IDs.

A twenty-year run should begin only after an official archive manifest establishes every expected volume/issue, including publication start dates, supplements/special issues and explicit gaps. Each real issue then needs reconciled article identities/types and complete attachment evidence. The current finite-issue ledger does not supply that archive closure. Database search totals and an agent's declared enumeration completion cannot fill the missing proof.

### Browser support requests

The six journal Rune packs include `scope.browser_support_origins: ["https://challenges.cloudflare.com"]`. A legitimate publisher page may load a Turnstile script or embedded challenge frame from this separate origin. Blocking that ordinary support request can produce an incompatible-browser/network message before the challenge can even be displayed.

This is a separate **browser subrequest** allowance. It does not add the host to top-level navigation, scholarly source, resolver, or download permissions. DNS/private-network checks, access-profile restrictions and source-operation policy still apply; redirects must be evaluated in their actual request context. A popup or new tab is a top-level navigation, not an embedded support frame. Unknown request context must not gain the allowance.

Loading a support script only permits the publisher's normal page flow to render. It does not solve a challenge, establish institutional entitlement, guarantee browser compatibility, bypass a source exclusion, or authorize an artifact download. The normal challenge/human-handoff rules remain in force. Existing jobs carry their original Rune digest and scope; changing a bundled pack alone does not silently revise an already-created mission.

## Model routing evidence

Fixed mode uses the available model/effort explicitly selected by the user. Automatic mode starts with Astra/high. A validated repetitive extraction/classification route may use Terra/low, and validated retrieval may use Sol/medium; failed reasoning/validation raises the effort. Unsupported model/effort selections fail explicitly.

The downgrade gate binds report bytes, runtime, normalized mission/Rune, task kind, exact fixture IDs and observed candidate model/effort. It requires at least five distinct empirical paired cases, both arms passing independent quality invariants, and no higher routed failure count. Synthetic tests and an editable `passed: true` field do not authorize a downgrade. Current runtime fingerprinting includes the runtime modules and shipped scholarly extraction profiles.

Historical five-paragraph Astra/Terra calibration and a separate automatic Terra extraction are documented in [evaluation.md](evaluation.md). They used an earlier runtime and a narrow extraction protocol. During this release review, the historical `routing-extract-calibration-final.json` was checked against current code and rejected with `reference_mission_digest_mismatch` and `evaluation_runtime_differs_from_current_runtime`. It must not authorize a 0.2 journal-collection downgrade. The whole-issue fixture above used fixed Astra/high and is not a paired routing experiment.

No architecture-superiority, token-price savings or monetary savings claim follows from these runs. Applicable prices and complete measured usage would be needed for a cost comparison, and paragraph extraction is not evidence of journal-retrieval equivalence.

## Local startup, Docker and migration

Local installation uses Python 3.12+, the scholarly extra for the bundled adapters, an available Codex installation/login for the subscription backend, and installed Chromium/OS libraries when browser actions are needed. See the root [README](../README.md) for installation. `ore doctor --browser` checks runtime availability without printing authentication secrets. `ore serve` starts the UI/API; API/UI mission creation produces a draft, and Run starts execution.

For service separation, [deploy/README.md](../deploy/README.md) and [compose.yaml](../deploy/compose.yaml) define PostgreSQL, a coordinator, two browser/network executors by default, and an isolated document validator. A host Codex worker supplies model decisions. The coordinator holds durable state and the Vault; executor containers hold private browser/download state and receive scoped assignment capabilities. The validator parses documents without publisher credentials. The Docker socket and host Codex authentication directory are not mounted into these services.

After configuring independent credentials in the protected deployment environment:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
ore worker --server http://127.0.0.1:8765 --parallel 2
```

`ORE_EXECUTOR_REPLICAS`, host model-worker parallelism, task budgets and profile browser limits all constrain effective parallelism. A human-controlled browser retains its executor assignment. Institutional access is determined by the executor's actual network/proxy and session, not by the host model worker. Containers may share an outbound IP. This deployment does not provide anonymity or imply a separate provider quota per container.

For an existing workspace, stop or pause active work, preserve a consistent database/state backup, install the target release, and run `ore db-upgrade` with the same configured database. Migration `0002` tags historical jobs `legacy_bounded` without rewriting their artifacts or claiming new evidence. New systematic claims require new evidence; migration and resume do not manufacture a sealed inventory. Keep the backup for rollback; the migration does not destructively remove history.

## MCP and durable user intervention

The local stdio adapter is started with:

```sh
ore mcp --server http://127.0.0.1:8765
```

The MCP client process receives its protected `ORE_AUTH_TOKEN` environment value or uses the local configured operator-token file; the token is not a tool argument. The adapter exposes `ore_status`, `ore_list_handoffs`, `ore_wait`, `ore_list_jobs`, `ore_create_mission` and `ore_job_action`. Creation produces a draft. Starting/resuming collection is an explicit action.

`ore_wait` polls for at most 20 seconds and returns on a state change or intervention request. It does not inject unsolicited messages into a chat. The client must call status/wait again and show returned job/handoff links. Users complete login, email verification, MFA or a provider challenge in the ORE UI's designated browser. Durable handoffs preserve the pending reason and task/session context; a lost browser DOM is not recreated merely by marking a handoff resolved.

This operator-facing MCP adapter is separate from the restricted internal Codex actor loop. The latter uses typed ORE actions and disables arbitrary shell/native browser/plugin access. MCP protocol support does not imply that every external client or notification workflow has been tested.

## Dedicated regression verification

The final dedicated run for this implementation passed **68 tests and 2 subtests**:

```sh
.venv/bin/python -m pytest -q tests/test_coverage.py tests/test_source_policy.py tests/test_scholarly.py
```

The checks include pagination/count/orphan-link failures, type/DOI disagreements, unknown versus explicitly empty supplements, actual-byte corruption, immutable/concurrent snapshots, wrong candidate URL/role/resource, authority fallback gates, missing required-source evidence, revision isolation, optional extension absence and preserved legacy contracts. These tests are offline fixtures, not empirical source-access results. Full-suite, build, Docker and real-journal results are maintained by the release operator below or in [VALIDATION.md](../VALIDATION.md).

## Autonomous browser access checks

When browser access is enabled, the LLM inspects the DOM and screenshot, reserves a bounded attempt, and can click an observed checkbox by coordinates even inside an iframe. ORE observes the asynchronous page transition before deciding whether the original target recovered. A checkbox click alone never establishes successful access.

```yaml
retrieval_policy:
  mode: official_first
  browser_fallback: true
on_challenge:
  max_attempts_per_episode: 3
  max_active_seconds: 120
  recovery_settle_seconds: 5
```

The selected access profile must permit browser use. The recovery window is capped at 10 seconds and the remaining reservation time; it uses the existing shared origin/auth-context budget. Duplicate reservations in one session are idempotent, while another worker's active attempt returns contention. Exhaustion retains the explicit handoff. No browser-identity masking, IP rotation or token fabrication is introduced.

`scripts/acceptance_agent_challenge.py` runs a real Astra/high actor against a local cross-origin iframe checkbox and checks HTTP 403→200 target recovery and exact text extraction, with one attempt and no human input. Its optional `--browser-assets .ore` uses installed Chromium binaries/libraries, not an operator browser profile. The measured run passed in 33.116 seconds with six decisions. This fixture is not Cloudflare; production challenge compatibility remains separately unverified.

## Final integration verification

The latest full Python suite passed **230 tests + 2 subtests** (86.32 seconds), UI **21 tests**, SDK **9 tests**. Fresh Python core/scholarly and npm installations are checked by the local release builder. The actual subscription Codex whole-issue fixture passed with 14 Astra/high decisions in 86.79 seconds, validating the hidden reference PDF, CSV and ZIP bytes.

Actual PubMed/API preflight received 69 indexed records for the four candidate issues and resolved eight research-prioritized DOI candidates through PMC and Unpaywall. It found nine main-PDF candidate records and three supplementary candidates; these include duplicate locations, unverified classifications and different publication versions. Candidate discovery does not prove file integrity or completeness. The report is `.ore/reports/api-fallback-preflight.json`; use `--records-per-journal 20` to reproduce the expanded sample.

A real Astra/high API/file sample subsequently retained three verified JAMA Cardiology supplementary PDFs and a main candidate requiring title/publication-version review. The actor made 15 decisions and no browser calls. Supplement bytes match independently checked PMC MD5 and vault SHA-256. The sample remains incomplete; `.ore/reports/api-file-sample.json` and `.ore/exports/jama-api-sample.zip` contain the result and review status.

The earlier official-issue experiments reached Cloudflare for each journal. The operator reports that ordinary Chrome opens JACC, while the fresh Playwright diagnostic reports automated-browser rejection. ORE's separate support-resource scope and resource pacing defects are fixed, but successful production challenge clearance is not claimed. The selected institutional profile now uses API/open-access first with automatic browser fallback disabled. Four old official-inventory jobs are paused and their repeated challenge requests cancelled with preserved history. They still have no sealed official issue inventories and have not passed the real-journal acceptance gate.

The previous Docker fixture verified two executor browsers, isolated downloads, validator service and recovery against its recorded image/runtime. New policy and scoped remote rate changes have Python regression coverage; that older image report is not proof of a freshly deployed container image. Production cgroup resource-limit enforcement remains unverified on this host. See [VALIDATION](../VALIDATION.md) for current evidence and limitations.

Python wheels/source archives and the npm SDK tarball are built locally; no registry upload is performed. Artifact hashes and installation results are in `dist/release-manifest.json` and `dist/SHA256SUMS`. This remains a release candidate, with twenty-year collection and the four complete real-issue gates unfulfilled.

## Separate-PC Chrome companion

The local coordinator can pair one mission with a dedicated tab in an existing Chrome profile. See [installation, scope and validation limits](chrome-companion.md). The Python wheel bundles the extension, served as an authenticated ZIP download from the handoff screen. This transport has a local Chromium fixture test; acceptance on the user’s PC and the four real publishers is still pending.

## ORE-owned native Chrome desktop

The wheel also bundles a Docker/X11 runtime for installed Chrome with screenshot and OS input. The console can attach it to an existing handoff and close it. A live local fixture verifies frame scope, scrolling, focus preservation, cookie-protected download, Chrome History provenance and exact-byte artifact registration. See [setup and limits](desktop-chrome.md) and [current validation](../VALIDATION.md); this is separate from the PC companion and from complete journal collection.
