# ORE 0.6.0rc2 validation — 2026-09-11

The running rc2 release adds chat-driven connection setup, protected provider enrollment actions, source-profile propagation, clearer planner errors and nested conversation lineage within purpose folders. [Release behavior](docs/release-0.6-rc2.md), [Korean-source evidence](docs/release-0.6-rc2-sources.md). Candidate implementation, package installation, running-service promotion and live collection are distinct checks; earlier release results below remain historical.

| Check | Current observed result | Evidence |
| --- | --- | --- |
| Full Python regression | 910 passed plus 6 subtests in 253.63 seconds; one dependency deprecation warning | `.ore/releases/0.6.0rc2/tests.log` |
| Web and SDK regression | 133 web tests and 16 SDK tests passed; production build passed | `.ore/releases/0.6.0rc2/build.log` |
| Browser UI fixtures | 18 checks passed, including connection/form and conversation flows; zero model calls | `web/test-results/ui-v06/report.json` |
| PDF and scholarly subset | 65 tests plus 6 subtests passed; CI fatal-error lint passed | `tests/test_vault.py`, `tests/test_scholarly.py` |
| Real JACC June 2024 planner | Six provider turns, 125.20 s, 232,124 observed tokens; one valid plan awaiting approval, zero errors; no collection started | `.ore/reports/planner-desktop-followup.json` |
| Deliberate planner budget failure | Two turns, 11.32 s, 58,136 tokens against a 50,000-token ceiling; actionable `budget_token_exhausted`, 8,136 in-flight overshoot retained | `.ore/reports/planner-desktop-followup.json` |
| Original desktop checkpoint | Both JACC previews eligible; origin recovery is scoped to the operator session and preserves challenge accounting | `.ore/reports/planner-desktop-followup.json` |
| Four-source bounded corpus | PubMed 5 main + 8 attachments; Scopus 5 + 15; KISS 5 + 8; RISS 5 + 3. All 49 unique files independently rehashed and format-checked | `.ore/rc2-completion/corpus-manifest.json`, `corpus-verification.json` |
| Korean live download/identity | Eight unique main PDFs and eight unique supplementary PDFs verified; a publisher PDF line-wrap mismatch was corrected and live verification completed | `.ore/v06-korean-completion/consolidated-acceptance.json`, `identity-final.json` |
| Release packages | Core-only/Claude-extra install, CLI, sdist rebuild and clean npm SDK install passed; 72 wheel Python modules match the workspace | `dist/v0.6-rc2/release-manifest.json`, `.ore/releases/0.6.0rc2/package-source-match.json` |
| Installed candidate | Six checks passed: chat-led Crossref setup without a configured model, protected email field, enrollment routes and a real bounded API check; zero model calls/collection jobs | `.ore/releases/0.6.0rc2/candidate-functional.json` |
| Running service | Port 8765 runs 0.6.0rc2; 11 installed UI/API checks passed, including all 52 existing progress routes and both legacy JACC desktop previews | `.ore/reports/ui-rc2-installed/report.json` |
| Deployment preservation | All 52 job rows and prior history preserved; three shutdown acknowledgements, one progress refresh and one authentication handoff were retained as additions | `.ore/releases/0.6.0rc2/deployment.json` |

The four-source archive contains **18 unique main PDFs and 31 supplementary/auxiliary files**, after deduplicating two Korean studies and three shared attachments. Archive: `.ore/reports/ore-four-source-sample-rc2.zip`, **36,223,354 bytes**, SHA-256 `9b3065176f5cd1fd967bf89af1088ce4dbc590dd5a4291902f7dc0186d3a64c1`. The 20 source memberships represent 18 different studies. Publisher/PMC evidence supports original-study eligibility and exact phrase matching. PubMed's two inaccessible publisher CDN aliases remain unresolved; corresponding PMC supplements are present, but byte-equivalence is unknown.

Source discovery and later deterministic ORE continuations were operator-assisted. The corpus is a verified bounded sample, not autonomous chat-to-corpus acceptance or a complete JACC month. Enrollment tests cover local DOM form preparation and reviewed effects; they do not prove fresh real-provider account issuance, approval or Claude subscription model execution. Screenshot-only Chrome desktops do not support automatic protected form filling. Provider quotas remain distinct from ORE budgets, and pending operations remain unavailable until verified.

The live planner checks created separate test conversations and reconnaissance jobs. Collection runners wrote isolated state; the final concurrent main-job digest changed during planner validation, while challenge digests remained equal. No claim of a globally unchanged main job table is made for that interval. Historical jobs, failed attempts and the incomplete 0.5 comparison are retained.

# Historical ORE 0.6.0rc1 promotion — 2026-09-11

At this historical promotion, the service on port **8765 was 0.6.0rc1**. All 48 existing jobs and the substantive conversation/plan/handoff/challenge records were preserved at promotion. The main service exposes the new chat, folder, connection and progress routes. This is a functional release; all-four-source completeness and superiority over standalone Codex are not established. [Release behavior](docs/release-0.6.md), [connection contracts](docs/connection-flow-0.6.md), [actual source results](docs/release-0.6-sources.md).

| Check | Observed result | Evidence |
| --- | --- | --- |
| Full Python regression | 814 passed plus 6 subtests in 218.80 seconds; one dependency deprecation warning | `.ore/reports/tests-v06-final.log` |
| Last setup-budget correction | After changing the setup limit to the actual `max_turns` field, all 38 connection tests passed in 16.75 seconds | `tests/test_connections.py` |
| Web and SDK | 79 web tests and 16 SDK tests passed; production build passed | `/tmp/ore-v06-web-final.log`, `packages/sdk/test/client-v06-release.log`, `web/test-results/build-v06-release.log` |
| Browser UI fixtures | 8 changing-state/reconnect/refresh/mobile checks passed | `web/test-results/ui-v06/report.json` |
| Installed service UI | 10 real-service checks passed; all 48 existing progress endpoints returned valid data; desktop/mobile without overflow or page errors | `.ore/reports/ui-v06-installed/report.json` |
| Real JACC planner reproduction | Original turn 108.65 s; Scopus follow-up 108.15 s; two valid plans, no error, no collection run | `.ore/v06-acceptance/planner-jacc-20260911T011437Z/planner-result.json` |
| Actual Claude SDK initialization | 7 checks passed using installed SDK 0.2.152; auth-required status, configured tool restrictions, connect/disconnect; zero model queries or login attempts | `.ore/v06-acceptance/claude-bootstrap.json` |
| Four-source retrieval | PubMed 5 main + 8 supplementary/auxiliary; Scopus 5 main + 15 supplements; KISS/RISS 0. Native runs and later operator-authored zero-model continuations are distinguished | `.ore/v06-sources/consolidated-acceptance.json` |
| Local artifacts | Core-only and Claude-extra installation, CLI, sdist rebuild and clean npm SDK install passed; no registry publication | `dist/v0.6/release-manifest.json` |
| Deployment preservation | Job and substantive history digests equal before/after; four expired task rows belonging to paused jobs retained | `.ore/releases/0.6.0rc1/deployment.json` |

The sample ZIP contains 33 verified files: ten main PDFs and 23 supplementary/auxiliary files. The ten studies have indexed membership, JATS original-study type, Methods and exact abstract-phrase evidence. Two publisher CDN attachment aliases remain inaccessible; corresponding PMC supplements were downloaded, but byte-equivalence to those aliases is unknown. KISS returned a blocked browser/search shell, and RISS exhausted the native allowance before search submission. These observations cannot establish zero matches or exhaustive supplementary coverage on every publisher site.

Native test usage was 577,912 observed tokens, including 98,029 in-flight overshoot. The later file continuations used zero model calls and did not reset the exhausted native runs. This is not an automatic-agent-only success claim. The retained 0.5 comparison below stopped after 40/72 cases and was not restarted or upgraded into a passing comparison.

Provider enrollment remains assisted: secret fields and official login are in the chat, while authentication/terms/form submission require authenticated operator control. Claude subscription model calls have not been live tested. Account quota and ORE request budget remain separate; unsupported provider counters are unknown. Interactive collection requiring arbitrary browser form actions can require an explicit capability review.

The runtime uses five local worker slots. This is not evidence of five deployed ordinary-Chrome virtual desktops; the existing diagnostic desktop capacity remains one. Deployment also configured the already installed Chromium cache and persistent browser libraries. The old server's open event streams required a second shutdown signal; no collection or planner was running, and substantive state remained unchanged. The new server has a 15-second graceful-shutdown deadline.

# ORE 0.5 candidate validation

The 0.5 implementation and local release checks are complete. The frozen 72-run comparison stopped after 40 completed cases (20/20 in each arm); it is incomplete and has no final comparative verdict. Four-journal whole-issue acceptance has not passed. This remains a candidate, without a claim of superiority over standalone Codex. See [0.5 contracts](docs/release-0.5.md) and [actual journal results](docs/release-0.5-journals.md).

## Current 0.5 measured checks — 2026-09-11

| Check | Measured result | Evidence |
| --- | --- | --- |
| Full Python regression | 689 passed plus 6 subtests, 186.15 seconds; two dependency deprecation warnings | `.ore/reports/tests-v05-final.log` |
| Web and SDK | 63 web tests and 14 SDK tests passed; TypeScript/Vite production build passed | `web/`, `packages/sdk/` |
| Actual native session | 6 native tool calls, same provider thread resumed, 3 exact Korean writes, zero duplicates; 19.617 seconds | `.ore/reports/agent-session-native-v05.json` |
| New runtime regression | 77 workflow tests and 42 adapter/public-stream tests passed; included in the full Python total | `.ore/reports/workflow-v05-tests.log`, `.ore/reports/native-adapter-v05-tests.xml` |
| Budget/runtime UI | 5 display-fixture checks and 5 screenshots, 1440/390 widths, light/dark and overshoot; zero page errors or horizontal overflow | `.ore/reports/ui-v05/report.json` |
| Local packages | Core-only install, CLI, sdist rebuild and clean npm SDK install passed; 62 wheel Python modules match the frozen workspace bytes | `dist/v0.5/`, `.ore/reports/build-v05-final.log`, `.ore/reports/package-v05-runtime-bytes.json` |
| Actual four-journal retrieval | 4 native turns, 100 tool calls, 172.31 seconds; 1 verified EHJ PDF and 0 verified supplements; no complete official issue | `.ore/journal-v05/native-acceptance.json` |
| Official issue inventory | All 4 publisher TOC probes returned HTTP 403; original-article and attachment denominators remain unknown | `.ore/journal-v05/official-access.json` |

The journal run preceded the subsequently tested local InvalidURL feedback and budget-pause fixes; it was not silently rerun or reclassified as a success. Its observed total was 1,238,864 tokens, including cached input and internal native completions. Three jobs exceeded their 350,000-token allowance while a provider turn was in flight and paused; those excess tokens remain reported. The saved EHJ PDF has independently checked file bytes, DOI, title and issue identity, but its automated publication-version field remains unknown. Pending WoS/Scopus APIs were excluded.

Native v2 defaults to Astra/high, with bounded failure escalation and independently verified deterministic reuse. No native cheaper-model/effort downshift is claimed. Public chat displays operational progress and accounting, not hidden reasoning. Schema/receipt validation is distinguished from independently verified corpus completeness.

At the 0.5 preparation checkpoint, the main service was 0.4.0rc1 on port 8765, with 47 jobs, 1 conversation and 7 substantive challenge records. The prepared 0.5 candidate uses separate state and port 8766; it has not yet been launched. Existing native Chrome capacity remains one diagnostic environment. The earlier five-executor HTTP fixture is not proof of five deployed native browsers. Historical sections below describe their recorded release state, including older 45-job snapshots; they describe their own historical checkpoint.

# ORE 0.4.0rc1 validation record

The 0.4 candidate integrates the redesigned operating console, real public streaming, adaptive admission/host provisioning, elapsed challenge accounting and collection progress. Local fixtures and successful UI/package checks remain separate from publisher access and whole-issue completeness. [Release guide](docs/release-0.4.md).

## Measured execution — 2026-09-11

| Check | Measured result | Evidence |
| --- | --- | --- |
| Full Python regression | 609 passed plus 6 subtests, 176.64 seconds; two dependency deprecation warnings | `.ore/reports/tests-v04-final.log` |
| Web and SDK | 59 web tests and 13 SDK tests passed; TypeScript/Vite production build passed | `web/`, `packages/sdk/` |
| Responsive visual inspection | 24 checks and 19 screenshots; 390/768/1024/1440 widths, light/dark, actual login and legacy routes; no horizontal overflow or page errors | `.ore/reports/ui-v04/report.json` |
| Actual public reply streaming | First durable public text after 6.698 seconds, provider completion after 15.248 seconds; 114 real deltas; exact final Korean answer; no execution created | `.ore/reports/public-stream-live.json` |
| Actual UI Stop/reload/Resume | 10 checks passed; first visible text after 5.53 seconds; 21 received characters restored exactly after Stop/reload; Resume completed a 1530-character answer; no duplicate user message, collection or page error | `.ore/reports/ui-v04/live-stream-report.json` |
| Post-comparison interruption replay | Reused the actual failed plan with a fresh fixture; 4 real provider turns, 36.77 seconds; 2 pre-stop file hashes preserved, 12/12 exact files, 0 duplicate writes; local interrupt acknowledged in 0.454 seconds | `.ore/reports/interrupt-postfix-live/report.json` |
| Actual executor pool | 5 distinct isolated executors registered, each performed HTTP fixture retrieval with exact content, then idle containers drained; 6/6 checks, 20.408 seconds | `.ore/reports/host-pool-live.json` |
| Actual Astra checkbox fixture | 6 real Astra/high decisions, 29.109 seconds; observed 403 → clicked the visible iframe checkbox → observed 200 → exact extracted text, one attempt, no human input | `.ore/challenge-v04-acceptance/20260910T215150Z-02538e/reports/agent-challenge.json` |
| Elapsed and ownership boundaries | Tests cover model-idle expiry, stale reservation settlement, stricter shared policies, preserved adaptive allocation, native observation fingerprints, trusted remote task linkage and unconfirmed physical termination | `tests/test_challenge_adaptation.py`, `tests/test_challenge_service.py`, `tests/test_desktop_runtime.py` |
| Retrieval deployment | Institutional default now API/OA-first with browser fallback; existing 45 job records unchanged | `.ore/reports/default-retrieval-v04.json` |
| Running service | 0.4.0rc1 on port 8765, operator worker cap 5; chat/jobs/connections/logo/font license/conversation routes returned 200; 45 job records preserved | `.ore/reports/live-service-v04.json` |
| Retained history compatibility | 45 job detail endpoints and 45 progress endpoints returned 200, 45 valid collection progress structures, unchanged status counts; 0 existing conversations | `.ore/reports/live-history-v04.json` |
| Official journal access | All 4 official 2024 issue TOC HTTP probes returned 403. Inventories and supplementary declarations remain unsealed | `.ore/reports/official-journal-access-v04.json` |
| API/OA sample | Bounded PubMed/PMC/Unpaywall preflight produced 1 main-PDF candidate and 2 other candidates; 0 downloaded/validated files | `.ore/reports/api-fallback-v04.json` |

The host-pool acceptance uses explicit `diagnostic_rlimit`, with per-process address-space/CPU limits and a PID cap, because this host lacks the required aggregate cgroup controllers. It exercises actual executor HTTP work, not native Chrome or validator parsing. Native Chrome remains limited to one diagnostic environment. The checkbox fixture is a controlled local website and **does not establish production Cloudflare clearance**.

The visual rich-conversation data is explicitly labelled a design fixture; the separate live-stream report uses actual Codex responses. Official journal 403 responses and resolver candidates are not reported as downloaded main PDFs or complete supplementary collections. Earlier profile records with `browser_fallback=false` below are historical; 0.4 changed the default while retaining explicit old job contracts.

The current service on port 8765 uses local task execution with a cap of five: `remote_execution=false`, zero ready executors, `native_desktop_limit=1`, and `native_desktop_capacity_verified=false`. Automatic isolated-executor provisioning requires the [host broker setup](docs/host-pool.md); the separate five-container acceptance above does not establish five deployed native browsers.

## Frozen 54-run architecture comparison

Six families (paragraph, paginated list, main/supplement roles, repeated extraction, interruption/restart with transient failure, and local verification/rate limiting) ran three repetitions in each of three arms. All 54 fresh cases executed; the order rotated and at most three cases shared provider capacity. Each arm had the same raw fetch/select/save/verify/wait handlers, five-primitive concurrency ceiling, 300-second elapsed allowance, 500,000-token allowance, 20 MB cumulative byte allowance and 180-primitive allowance. The standalone native tool host could batch calls; its observed peak was one, versus five in both ORE arms. The hidden independent grader required exact output bytes/values, successful GETs of every required source, observed verification and Retry-After compliance.

| Arm | Full pass | Output/protocol pass | Median wall seconds, all 18 runs | Observed total tokens |
| --- | ---: | ---: | ---: | ---: |
| Standalone Codex | 18/18 | 18/18 | 26.79 | 1,360,660 |
| ORE fixed | 15/18 | 15/18 | 67.80 | ≥ 2,945,936 |
| ORE adaptive | 13/18 | 14/18 | 71.94 | 2,996,041 |

All eight failures remain in the cohort: one adaptive repetition-0 restart had an unconfirmed deterministic worker; fixed restart repetition 1 and adaptive restart repetition 2 timed out; verification timed out for fixed repetitions 0/2 and adaptive repetitions 0/1/2. Adaptive verification repetition 1 had correct outputs and protocol evidence but failed completion/budget requirements. All measured runs had zero duplicate writes. The later worker-launch race repair is separate evidence and does not turn the frozen failure into a pass.

The 12 identical requests that fully passed every arm are the three repetitions of the four simpler families. Standalone/fixed/adaptive median wall times on this matched subset were **26.79/60.79/65.97 seconds**; total tokens were **783,309/493,256/541,799**. Fixed/adaptive paired geometric wall-time ratios versus standalone were **2.07/2.20**. These conditional figures do not replace the full-cohort failure rates. They show fewer recorded tokens on these requests with planning overhead; they do not establish a general speed, quality or price advantage.

Model selection was Astra throughout: 144 provider transport turns at high effort and one adaptive failure escalation at xhigh. **No cheaper model or effort downshift was observed.** Transport turns and token-usage notifications are not internal model-invocation identities. Numeric cumulative provider usage includes internal native completions. Complete accounting was available for 53/54 runs; the fixed repetition-2 verification timeout cancelled a repair before a fresh usage update, so its arm total is an observed lower bound. Cached input totaled 1,215,104/2,141,184/2,253,440 tokens across standalone/fixed/adaptive. Token totals cannot be converted into subscription invoices or cost savings from these data.

The frozen report's phrase “common hard token ... ceilings are enforced” was too strong. The implementation checks token usage at primitive-dispatch boundaries; already-running provider turns can overshoot. All seven timeout cases exceeded 500,000 observed tokens and failed `budget_pass`; the largest measured total was 598,130. Time/byte/primitive/concurrency checks also gate a pass. The release harness now corrects this wording without changing behavior. The verification fixture uses raw HTTP/form primitives that bypass `BrowserManager` challenge and origin-rate controllers: it is **not** a test of Cloudflare clearance or the elapsed challenge policy.

All 54 start/end fingerprints matched runtime digest `96961e2c324854449334ac1cc8dc52dffb64d802b1617894adb21c2e0bee7c1c`. Evidence: `.ore/reports/architecture-benchmark-frozen/report.json`, `summary.json`, `summary.md`, and `provenance/`. The raw report SHA-256 is `55ab7332bab5c74033e95d9b2f10ea02b42cca3f0553931b79fc47e4c4bd9335`. Exact measured scripts and the full runtime/dependency manifest are retained in `provenance/`; tool-host pilots and pre-freeze aborted cohorts are excluded. The measured harness SHA-256 is `37ab7c2ffe14bb781d03145091e8ff9a6df13924aa6625cb892a3ebb59213b23`; the wording-corrected release harness SHA-256 is `03509d0b27cee039bc92f09df5efbc75b4be4e609bf5d0183d910159b597c6c9`.


## Post-comparison interruption repair

The frozen restart failure exposed an unacknowledged local task between its SQL claim and coroutine entry. The release tracks claim publication and records explicit proof when its own coroutine never entered. A delayed claim rejected during shutdown is settled atomically with its original task/fence/worker ownership. Storage errors retain that proof for retry while local worker maps are cleaned; uncertain model, remote and resource termination still remains unconfirmed. Eight deterministic regression cases cover these boundaries, including the two additional shutdown/storage issues found by independent review.

The separate actual replay reused the failed run's model-authored plan, changing only the ephemeral fixture origin and reproducing its observed reconnaissance HTTP 503. It performed a real Engine stop/restart, preserved two existing file hashes, and produced all twelve exact files without duplicate writes. One interrupted task actually used the new `local_coroutine_not_started` acknowledgement. Its recovery/audit nodes made four real provider turns; planning was reused, so its 36.77 seconds is not a comparable replacement benchmark result. The original failed row and all 54 frozen fingerprints remain unchanged. The applied patch and source hashes are in `.ore/reports/interrupt-fix-applied.json`; the isolated diagnosis is `.ore/interrupt-fix-dev/diagnosis.json`.

---

# Historical ORE 0.3.0rc1 validation record

0.3 implements multi-turn conversational planning, a chat-first UI, an extensible workflow runtime, automatic paired model adaptation and durable interruption. Its actual UI/model/container acceptance is separate from whole-journal completeness. Package artifacts are local release candidates; no registry publication is part of this run. See [the 0.3 guide](docs/release-0.3.md).

## Measured 0.3 execution — 2026-09-11

| Check | Measured result | Evidence |
| --- | --- | --- |
| Full Python regression | **527 passed + 6 subtests**, 164.13 seconds; two dependency deprecation warnings | `.ore/reports/tests-chat-final.log` |
| UI / SDK | 49 UI tests and 11 SDK tests passed; production TypeScript/Vite build passed | `web/`, `packages/sdk/` |
| Real conversational Codex | Korean clarification, real page reconnaissance, compiled four-tool plan, explicit test approval, exact text artifact; zero model calls during deterministic execution | `.ore/reports/conversation-live.json` |
| Live chat UI | Real Codex clarification/plan, explicit approval, exact supplied JSON download, history/reload/mobile/legacy views; all 17 follow-up Stop/reload checks passed, including actual provider acknowledgement and stable idle event cursor | `.ore/reports/chat-ui-live.json`, `.ore/reports/chat-ui-results.png` |
| Real parallel generated code | Three distinct Docker containers, peak three running concurrently, exact sums 3/7/11, verified JSON artifact, zero model calls; 2.96 seconds for the collection phase | `.ore/reports/workflow-runtime-live.json` |
| Real container Stop | Started a 60-second program, interrupted it, independently verified no running container and confirmed paused state; previously completed artifact preserved | same runtime report |
| JACC June 2024 planning | Four actual Astra/high turns, 46.712 seconds; first source request was official JACC archive, HTTP403; then trusted scholarly profile inspection and an explicit access-gap question. No plan approval, issue seal or file collection | `.ore/reports/jacc-month-plan-live.json` |
| Local packages | Core-only and scholarly clean installs, CLI, SDK install/import and source-distribution rebuild passed; artifacts include the new chat UI and workflow modules | `dist/release-manifest.json`, `dist/SHA256SUMS`, `.ore/reports/build-chat-release.log` |
| Updated running service | 8765 serves chat and conversation/capability APIs, with three local workers; all 45 existing job states preserved | `.ore/reports/chat-live-service.json` |
| Exact month inventory | Controlled HTML fixtures test June1 inclusive to July1 exclusive, required pagination/count/link/range coverage and rejection of missing evidence | `tests/test_archive_workflow.py` |
| Adaptive routing | Controlled backend tests verify paired shadow calibration, downshift, failure upshift, contract invalidation and budgets. Actual live-model cost savings were not measured | `tests/test_workflow.py` |
| Interruption robustness | Tests cover stale commits, provider false/unknown acknowledgements, orphan-container uncertainty, remote lost-receipt retries, heartbeat during blocking I/O and settlement before acknowledgement | `tests/test_interrupt_safety.py`, `tests/test_workflow.py`, `tests/test_executor.py`, `tests/test_conversation.py` |

The generated-code acceptance explicitly used `ORE_CODE_RESOURCE_MODE=rlimit` because this Docker daemon lacks the default cgroup memory/CPU controllers. It validates actual process/container isolation and per-process limits, not aggregate cgroup memory accounting. See the deployment guide for that distinction.

During live validation we found and fixed invalid natural-language checks in plans, stale execution receipts after descendant revisions, repeated progress events caused by volatile timestamps, an older completed run overwriting a stopped planner, and false physical-stop assumptions. The final reports distinguish verified execution from planning and fixture-only checks. The preserved real four-journal inventories below remain unsealed; none of these new feature tests establishes complete published main PDFs and all supplementary files.

---

# Historical ORE 0.2.0rc1 validation record

The 0.2 release candidate implements the coverage, isolated execution and user handoff changes. It is **not a verified complete journal corpus**. Latest measured checks in this workspace on 2026-09-11; all reports under `.ore/` stay local and are excluded from distributions. Prior 0.1 measurements are preserved in [the historical record](docs/validation-0.1.md).

The latest [real 2024 journal test](docs/real-journal-test-2024.md) includes twelve actual Astra checkbox clicks after one explicit user-authorized budget renewal. None passed the publisher challenge. Actual retained files and operator-directed recoveries are reported separately from complete issue collection.

## Measured 0.2 execution

| Check | Actual result | Evidence |
| --- | --- | --- |
| Python regression suite | **439 passed + 6 subtests**, 121.76 seconds; two dependency deprecation warnings | `.venv/bin/pytest -q`; `.ore/reports/tests-desktop-final.log` |
| UI and SDK tests | **37 UI + 9 SDK** tests passed; production UI build passed | `web/`, `packages/sdk/` |
| Operator MCP process | Actual stdio initialize, six-tool list and live authenticated status passed | `.ore/reports/mcp-v02.json` |
| Real subscription Codex paragraph | Astra/high, five decisions, 31.14 seconds; exact paragraph and verified artifact | `.ore/acceptance-v02/reports/codex-acceptance.json` |
| Real subscription Codex whole-issue fixture | Astra/high, 14 decisions, 86.79 seconds; two TOC entries, one original and one excluded editorial; original PDF, CSV and ZIP matched independent hidden hashes | `.ore/acceptance-scholarly-v02/20260910T141520Z-ff7d6e/reports/whole-issue-fixture.json` |
| Two Docker executors | Two actual Chromium sessions, HTTP downloads with isolated cookies, original-byte upload verification; coordinator browser did not start | `.ore/reports/executor-container-acceptance.json` |
| Durable browser restart | Same browser survived coordinator restart; actual executor-process loss invalidated the old browser, and a human-controlled replacement was adopted by the original task with a higher fence | same executor report |
| Isolated validation | Separate token and internal network, no operator/provider credentials or state mount; actual downloads used validator service | same executor report |
| Operator UI | Seven live browser checks passed, zero page errors: deep-link login, existing-account setup, pending state, reload, ETA wait, institution readiness and search-only exclusion | `.ore/reports/ui-v02.json`, `.ore/reports/ui-v02.png` |
| Local package installation | Python core-only and scholarly installs, bundled UI, CLI, source archive rebuild and npm tarball install passed | `dist/release-manifest.json`, `dist/SHA256SUMS` |
| Four actual 2024 journal issue attempts | **All four official issue inventories remain unsealed, 0 systematic-job artifacts**; native Chrome now opens the issue pages (see below); actual bounded samples retained **4 main PDFs + 5 supplements**, with manuscript/version and missing-inventory gaps | [Current real test](docs/real-journal-test-2024.md), `.ore/reports/real-2024-journal-test.json` |

The controlled whole-issue fixture is an end-to-end model run with expected identities and file hashes held outside the agent. It does not validate current publisher selectors, subscription entitlement or a real journal issue. Main PDF has published-version and DOI/title evidence; CSV/ZIP originals are bound to the official fixture attachment manifest, not mislabeled as journal article PDFs.

## Reported browser error and repair

The user reported `Incompatible browser extension or network configuration`. Actual ORE request-denial logs showed that the mission origin scope blocked `https://challenges.cloudflare.com/turnstile/.../api.js` in all four journal sessions. This was an ORE configuration defect, not evidence that the user's browser extensions were at fault.

The fix introduces explicit browser support origins for subresources within an allowed page, preserving source, profile, DNS, popup/top-level navigation and download restrictions. Four existing handoff IDs were retained and their browser processes recreated with the explicitly updated institutional profile. Observations after the repair show zero ORE-blocked resources; Cloudflare security verification is still unresolved. Thus this repair is not a successful publisher access or corpus acceptance. Evidence: `.ore/reports/journal-browser-diagnostic-v02.json`, `journal-browser-repair-v02.json`, `journal-browser-after-repair-v02.json`. Real Chromium regression verifies loaded support script, rejected popup, rejected attachment and visible denial diagnostics. A separate actual browser test closes/recreates the handoff browser, resolves a fixture challenge by human control, reclaims the original task with a higher fence and successfully observes that replacement through ToolRuntime. The resumed agent state exposes only its owned replacement session; another task cannot discover or use it.

## Browser loop diagnosis and API/open-access preference

The official Cloudflare diagnostic, visited in a fresh browser without ORE request routing, reported `Automated Browser Detected`. Server and browser clocks were consistent with HTTPS server time; the linked Reddit clock-error case did not match this machine. The operator reports that ordinary Chrome/Edge on the same network opens JACC. This is operator-reported regular-browser access, not an independently verified entitlement or browser-session transfer. Evidence: `.ore/reports/cloudflare-compatibility.json`, `cloudflare-loop-clock.json`.

The unrelated ORE pacing defect is corrected: static/support subresources use a separate 0.1-second default lane, while navigation/API/data/file traffic retains main pacing and HTTP 429 blocks both lanes. Actual browser timing, cookies, remote coordinator floors and shared backoff were tested. No browser identity or fingerprint override was introduced; this fix does not claim successful Cloudflare clearance. Evidence: `.ore/reports/browser-resource-pacing-validation.json`.

`retrieval_policy.mode=api_open_access_first` and `browser_fallback=false` are now saved on `institution-onboarding`. The profile restriction also applies to older mission contracts. Newly created missions snapshot the profile preference, and actors receive an admitted/skipped operation plan. Pending or excluded sources are filtered before requests. Automatic browser creation/actions are rejected before local or remote allocation when fallback is disabled. Both source admission and sealed evidence gates remain enforced. The effective-policy refinement passed 49 focused route/coverage tests; the final full suite includes this change, the PubMed date fix and a false→true fallback-toggle regression.

Actual API preflight exposed a PubMed EFetch date bug: concatenating Year/Month/Day produced `2024Jan2`, which the year matcher missed. The adapter now reads the structured Year first and falls back to MedlineDate. A regression covers structured dates, spanning MedlineDate, precedence and missing years.

The four previous challenge requests were cancelled with an explicit route-selection explanation, their original jobs paused, and request IDs/history retained. No challenge episode budget was reset. The updated local service reports zero active journal handoffs and zero browser sessions. Evidence: `.ore/reports/api-route-selection.json`, `api-route-live-service.json`. The original official issue inventories remain unverified; selecting an alternative retrieval order does not establish a comprehensive corpus.

## Current browser repair — 2026-09-11

A fresh unchanged Chromium diagnostic without ORE routing or operator cookies again explicitly reported `Automated Browser Detected`. Its two failed subdomain DNS probes and two PAT-path HTTP 401 responses are expected observations according to current Cloudflare documentation, not demonstrated causes of the loop. The earlier claim that the `brunhild` probe was a required broken dependency has been corrected. No new production challenge attempt or budget renewal occurred in this repair.

Native DNS-error propagation, conservative network/runtime classification, concurrent profile persistence and visible-only authentication checks have been corrected. These code fixes do not establish production challenge clearance. The operator's working ordinary browser has not been connected to ORE. See [the current diagnosis and implementation evidence](docs/browser-repair-2026-09-11.md). Current packages were rebuilt with the updated console and passed clean-install checks; no registry publication occurred. Live deployment evidence: `.ore/reports/browser-repair-live-service.json`.

## Actual API route preflight

The research-prioritized run of `scripts/acceptance_api_fallback.py --state-dir .ore --profile institution-onboarding --records-per-journal 20` received 69 PubMed metadata records for the four candidate issues (18 JACC, 18 EHJ, 13 Circulation, 20 JAMA Cardiology). It resolved two candidate DOIs per journal through PMC and Unpaywall: 31 GET requests, 704,866 response bytes, approximately 26 seconds. The observed candidates include nine main-PDF records and three JATS-linked supplementary files, with duplicates, an upstream JPG mislabeled as PDF, and differing publication-version labels retained. These counts are not validated files or an official issue denominator. Known indexed editorials, letters and corrections are excluded from this resolver shortlist; remaining index types still do not certify original-research eligibility. Evidence: `.ore/reports/api-fallback-preflight.json`.

## Actual subscription Codex file retrieval

A real Astra/high actor ran the bounded JAMA Cardiology DOI `10.1001/jamacardio.2023.4147` sample through PMC and Unpaywall with **15 decisions, 182.98 seconds and zero automatic browser calls**. It downloaded three supplementary PDFs from JATS-linked PMC dataset URLs and one main-PDF candidate from the repository fallback after the publisher route was unavailable. The three supplements passed file validation and independent equality against both stored SHA-256 and PMC-supplied MD5. Total retained bytes: 4,660,550.

In that historical run, the main candidate had valid PDF integrity and the requested DOI, but its first-page title did not match; Unpaywall labels it `submitted_manuscript`. It remains **needs_review**, not a verified published final copy. Original-research classification and a complete official publisher supplement manifest remain unverified, so the job is **needs_review** and the corpus audit **incomplete**. The sample also exposed a reporting bug: unclassified records could incorrectly produce `supplement_discovery_complete=true`; this now stays false until eligibility and supplement discovery are resolved.

Evidence: `.ore/reports/api-file-sample.json`; downloadable local sample: `.ore/exports/jama-api-sample.zip`. This is an actual model-driven API/file retrieval, separate from the controlled whole-issue fixture and the four paused systematic issue jobs.

## Autonomous ordinary access-check execution

`BrowserManager` now waits for asynchronous target recovery after an ordinary reserved action: `on_challenge.recovery_settle_seconds` defaults to 5 seconds, caps at 10 seconds and cannot exceed the existing reservation expiry. This observation time is charged to the same shared episode. Repeating `challenge` for the same live reservation is idempotent; another session's reservation produces retryable contention rather than a human takeover. Expired/exhausted budgets never reset. The actor instructions explicitly support screenshot-guided checkbox coordinates inside iframes and require observed target recovery.

A real subscription Astra/high actor opened a controlled local page that returned HTTP 403, identified and clicked the checkbox inside an iframe served from another origin, waited through the 700 ms page transition, reached the same issue URL with HTTP 200, and extracted the exact target paragraph. **6 model decisions, 33.116 seconds, one reserved attempt, zero human handoffs**, with no scripted model decisions or manual browser input. The observed click was `(118, 274)`. Evidence: `.ore/acceptance-agent-challenge/20260910T145111Z-845d08/reports/agent-challenge.json`.

This is an ordinary checkbox/iframe execution test, **not a production Cloudflare challenge test**. It proves that Astra can select and execute the visible interaction through ORE and continue without user help when the site accepts it. It does not establish that any of the four publishers accepts the current automated browser. The selected institutional API-first preference and prior challenge episodes were not changed for this test.

## Completion boundaries

- Final 0.2 promotion requires one actual 2024 regular issue with original research and supplements from **each** of JACC, EHJ, Circulation and JAMA Cardiology, independently reconciled against all TOC records and original file requirements. That four-journal gate has not passed.
- Twenty-year collection has not run. Explicit finite issue scope is supported; an authenticated publisher archive inventory spanning all years is not independently sealed yet. Global database recall stays unknown.
- Article type, supplement absence and publication version require recognized source evidence. An unfamiliar template or ambiguous category remains a gap. PMC/Unpaywall links do not establish publisher supplement completeness.
- The current runtime invalidates historical routing calibration. Automatic mode defaults to Astra/high until a report passes against this exact runtime; no generalized model cost or quality advantage has been measured.
- WoS search remains approval pending. Stored Scopus readiness covers the earlier exact-DOI metadata query only. Browser readiness is separate from API approval and PDF entitlement. The 0.2 migration references earlier actual reports; it did not repeat account applications or claim fresh API authorization.
- Codex uses constrained structured action turns. Native shell, computer tools, external MCP/plugins and code-mode host remain disabled in the collection loop. The separate operator MCP adapter only exposes ORE status and handoff controls.
- Production Compose includes CPU/memory/PID caps, but this workspace Docker daemon cannot apply its cgroup v2 controls. The latest functional container fixture explicitly disabled those caps and recorded `resource_limit_validation=not_validated`; read-only filesystems, internal validator network, noexec temp storage and credential separation remained enabled. Production resource-limit enforcement is not verified on this host.
- Docker tests used fixtures without model calls. The actual model fixture and actual multi-container execution are separately reported; no combined live publisher/model/two-container success is claimed.
- No anonymous access, challenge circumvention, global indexing completeness, automatic paid-model fallback, public registry publication or 20-year final corpus is claimed.

## Reproduction

See [the release guide](docs/release-0.2.md) and [deployment instructions](deploy/README.md). `scripts/acceptance_issue.py` keeps a reviewed expected issue manifest outside the agent and can verify an existing resumed job. Models and live sites are exercised only by explicit acceptance scripts; ordinary pytest tests use fixtures.

## Separate-PC Chrome companion

The operator clarified that the working Chrome runs on a separate PC. ORE now includes an optional Chrome extension and an expiring, one-mission pairing channel. Existing handoffs can switch to its dedicated tab. Pairing does not reset challenge budgets or enable automatic browsing when the profile disables it.

A local test loads the actual extension into Chromium, attaches through the authenticated WebSocket, reads DOM and a high-density screenshot, resolves an ordinary fixture checkbox using the existing budget, records a snapshot with the observed top-frame HTTP 200, and retrieves exact bytes from a fixture endpoint requiring a Chrome-only HttpOnly cookie. The coordinator does not launch its own browser and receives no cookie/profile export. Disconnect pauses the job, restores a user request and prevents silent server-browser fallback. Pair replay, expiry, mission revision and webpage-origin rejection are tested separately.

The Chrome protocol checks also passed **5 Node tests**, and the updated console passed **26 UI tests**. The user-side Chrome and the real publishers have **not** been tested through this transport. This is an actual extension/fixture test, not a new LLM run or production Cloudflare success. The extension's scripts are included in runtime fingerprints so historical routing calibration cannot be reused after client execution code changes. See [installation and limits](docs/chrome-companion.md). Evidence: `.ore/reports/tests-companion-release.log`, `.ore/reports/companion-web-tests.log`.

## ORE-owned native Chrome — 2026-09-11

Installed Chrome runs inside ORE's isolated X11 container. The existing subscription Codex/Astra backend receives native screenshots and uses ORE mouse/keyboard actions. Playwright/CDP/browser extensions are not used in these tests. [Setup and limits](docs/desktop-chrome.md).

| Actual target | Native result | LLM/challenge evidence |
| --- | --- | --- |
| JACC 83(1), 2 January 2024 | Exact issue page loaded | No LLM call or challenge interaction needed in this run; 19.50 seconds |
| Circulation 149(1), 2024 | Exact issue page loaded | No LLM call or challenge interaction needed in this run; 18.60 seconds |
| European Heart Journal 45(1), 1 January 2024 | Exact issue page loaded | Actual Astra/high, five decisions, one reserved checkbox click, episode resolved; 56.93 seconds |
| JAMA Cardiology 9(1), January 2024 | Exact issue page loaded | Actual Astra/high, one reserved checkbox click, episode resolved; 59.14 seconds |

Source reports: `.ore/desktop-validation/20260910T193044Z-df211c/reports/desktop-diagnosis.json`, `20260910T192713Z-d0331f/reports/desktop-diagnosis.json`, and `20260910T193214Z-2fc070/reports/desktop-diagnosis.json` under the same validation root. Earlier failures are retained separately. In particular, an earlier JAMA run resolved its challenge but the final body-only text check missed the journal name after scrolling; the final diagnostic uses the exact checkpoint plus observed branding, volume and issue.

A separate actual native fixture passed both iframe-policy cases, scrolling, focus preservation across observations, a cookie-protected CSV download, its two-URL redirect receipt, exact SHA-256 and ORE artifact registration. This used scripted OS input and zero model calls. Report: `.ore/reports/desktop-acceptance.json`. The file was 58 bytes with SHA-256 `3ef866a527b765d288c2e7105d1ef2f8e177bdf236e4be98c4d69bc58f3a3d43`; it is not a journal PDF.

Corrections include explicit native frame origins, waiting without consuming challenge attempts, bounded recovery observation, page focus restoration without Escape, repeated-coordinate clicks, reaping clipboard helper processes, OCR thread limits, and consistent private copies of Chrome's locked History database. The source profile is not modified when reading receipts.

These are anonymous page-access tests, not subscription-entitlement or complete-issue collection tests. No main PDF or supplement was collected by these journal compatibility runs. Original institutional jobs, their six-attempt exhausted histories, and the API/open-access preference are preserved. The console supports explicit native attach/close on the same handoff. This host uses one-at-a-time watchdog diagnostics with a five-minute session lifetime; native parallel execution under working cgroups is not established by these single-session tests.

Final combined report: `.ore/reports/desktop-validation-final.json`. The updated live service passed native attach/close on the original JACC request with job/profile/challenge accounting preserved. A real console check passed login → native attach → 1280×800 PNG canvas stream → close, with zero browser errors (`.ore/reports/desktop-live-ui.json`, screenshot `desktop-live-ui.png`).
