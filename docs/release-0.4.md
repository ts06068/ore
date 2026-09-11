# ORE 0.4 release candidate

ORE 0.4 brings the conversation, plan, execution and collection evidence into one operating console. It retains existing jobs, files, routes and explicit approved policies. This is a local release candidate; no registry publication is performed.

## Console

The console uses the original ORE vector geometry, locally served Inter and Noto Sans KR fonts, a light porcelain/graphite palette and a persistent dark-mode preference. Navigation, login, connections, missions, handoffs and chat share the same tokens. The conversation column stays readable on desktop; navigation and results become drawers on smaller screens. Font licenses and provenance ship with the assets.

Each user turn owns its public answer, questions, plan, execution explanations and results. Public model text arrives through real provider deltas with durable sequence numbers. Reconnect and reload replay persisted data; they do not invent a typing animation. Private model reasoning and raw tool arguments are not exposed as a thought transcript. Provisional streamed text cannot approve a plan: the complete structured response must validate first.

Plan mode asks questions and prepares an inspectable plan. Execute mode follows the existing approval contract. Stop invalidates execution authority immediately and retains separate acknowledgements until model/executor/container work has actually stopped. A paused run can resume with verified outputs preserved. Results and collection progress are available alongside the conversation.

## Adaptive execution

New missions begin with `parallelism.initial=5` and `parallelism.mode=adaptive`. The approved `budget.max_agent_workers` is the mission ceiling; the operator's `ORE_MAX_WORKERS`, global ceiling, ready tasks, origin limits and available executor resources can constrain it further. Increasing the approved ceiling requires a changed plan. Changing the soft target does not revise the mission or cancel completed work. For example, a plan ceiling of 10 also needs an operator cap of at least 10 (`ore serve --workers 10`); its initial soft target can still be 5.

The scheduler samples every 15 seconds, requires 3 stable windows before growing by one slot, and reduces demand under errors, 429 responses or resource pressure. Foreach work refills available slots when a child finishes. Fixed mode remains available. Scheduler state distinguishes desired, running and actually ready workers; a requested container count is not a readiness proof.

The trusted host pool provisions fixed-image isolated browser/network executors through a dedicated outbound broker channel. Workers register with short-lived single-use launch tokens and become ready after health reports. Idle drain protects active work, browser sessions and pending user interaction. Provider credentials and the Docker socket remain outside executor containers. See [host-pool deployment](host-pool.md) for the exact operating and resource-enforcement boundaries.

Deterministic tools and compiled workflows make no model calls. Novel work starts with the high-quality supported route. Empirical per-family validation can permit cheaper model/effort choices; failed checks or changed contracts invalidate that evidence and escalate. Model labels alone are not a quality or cost measurement.

## Actual finite comparison

The frozen comparison executed 54 real model runs: six local task families, three repetitions, and standalone Codex/fixed ORE/adaptive ORE. Fresh fixtures exposed the same five raw primitives; standalone used the native Codex tool host, while ORE used actual model-authored workflows. Cases ran three at a time with rotated arm order. Every failed run remains in these totals.

| Arm | Full pass | Output/protocol pass | Median wall seconds, all 18 runs | Observed total tokens |
| --- | ---: | ---: | ---: | ---: |
| Standalone Codex | 18/18 | 18/18 | 26.79 | 1,360,660 |
| ORE fixed | 15/18 | 15/18 | 67.80 | ≥ 2,945,936 |
| ORE adaptive | 13/18 | 14/18 | 71.94 | 2,996,041 |

The same 12 requests passed all three arms. Their median wall times were 26.79/60.79/65.97 seconds and total tokens were 783,309/493,256/541,799 respectively. On that conditional subset, ORE used fewer total tokens but took longer. This does not establish a lower bill: cached-input and output-token mixes differ, and these are subscription measurements rather than API invoices. Across all 18 requests per arm, ORE had more failures and higher observed total token use.

No model downshift occurred: 144 recorded provider transport turns used Astra/high and one adaptive failure escalation used Astra/xhigh. A transport turn can contain multiple native completions. Seven cases timed out or exceeded budget; one retained adaptive restart failure exposed a local worker-launch acknowledgement race. The frozen result is not rewritten by the later race fix.

Token checks occur before primitive dispatch; an in-flight provider turn can overshoot the 500,000-token allowance. Overshoot remains measured and fails `budget_pass`. One cancelled fixed-arm repair lacked a fresh usage update, making that arm's token total a lower bound; 53 of 54 runs had complete accounting. The local verification primitives bypass the browser challenge/origin controllers, so this comparison does not measure the elapsed challenge policy or establish Cloudflare clearance. See [the validation record](../VALIDATION.md) for provenance and failure details.

## Challenge clock and browser lifetime

New policies use an elapsed clock from the first automatic challenge observation, initially 3 attempts/120 seconds. Model thinking, navigation and waiting count. The same origin and access principal share the episode across tasks and sessions. New sessions and process restarts do not restart an exhausted episode.

Verified intermediate progress or comparable recorded successful recoveries can extend an unexpired episode in steps of one attempt/60 seconds, to the approved maximum of 6 attempts/300 seconds. An unchanged challenge, incompatible environment, authentication requirement or 429 can stop earlier. An independent coordinator watchdog transfers browser control when the deadline expires, even while the model is thinking. Browser inputs also check the persisted deadline. A late page transition is not recorded as an automatic success within budget.

The native desktop has a separate coordinator-owned renewable session lease, bounded by the mission time budget and a one-hour session ceiling. A lease cannot revive an expired desktop or reset CPU accounting. Its challenge timer is independent. The current host's explicit diagnostic watchdog mode still permits one native Chrome at a time and keeps its 600-second CPU budget; it is not evidence of five native Chrome environments with cgroup limits.

Existing explicit challenge policies retain their legacy active-time behavior. Existing exhausted budgets are not silently enlarged. Automatic ordinary visible interactions do not guarantee that a third-party challenge will accept the session.

## Retrieval and progress

New default retrieval ranks permitted API/open-access routes first, with browser fallback enabled. The official journal/publisher inventory remains separate from file retrieval order. Pending or excluded sources are omitted with recorded reasons. A route candidate is not a verified downloaded file.

Progress distinguishes discovered, included, excluded, unresolved and completely fulfilled resources, and required versus verified file roles. Unknown supplementary declarations are different from confirmed absence. Corpus percentage remains unknown until the denominator is sealed. Workflow-node completion alone cannot establish a complete corpus. ETA is conditional on observed throughput, dependencies, ready capacity and a known remaining workload; insufficient evidence stays unestimated.

The 4 target journals remain subject to their actual access and inventory evidence. UI fixtures, local challenge forms and API candidate discovery do not establish successful whole-issue PDF/supplement collection.

## Installation and evidence

```sh
uv sync --locked --extra dev --extra scholarly
codex login status
uv run ore serve --workers 5
```

Open `http://127.0.0.1:8765` and authenticate with the local `.ore/operator.token`. The Codex backend uses the local signed-in Codex installation. API and local-model backends remain optional.

The core is `0.4.0rc1`, the TypeScript SDK is `0.4.0-rc.1`, and the compatible scholarly adapter remains `0.2.0rc1`. The release build packages the console, fonts, logo, Python modules and SDK, checks clean installations, and writes hashes:

```sh
ORE_DOCKER_WORKSPACE=/HOST/PATH/TO/ore .venv/bin/python scripts/build_release.py
```

[VALIDATION.md](../VALIDATION.md) records actual executed checks, comparison results and limitations. The finite comparison uses the same raw primitives for standalone Codex, ORE fixed and ORE adaptive; its hidden grader checks original bytes/extracted values. It does not infer global recall or translate subscription tokens into an API invoice.
