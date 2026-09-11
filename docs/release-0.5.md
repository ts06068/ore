# ORE 0.5 candidate: native execution and measured reuse

ORE 0.5 replaces the compulsory one-JSON-action-per-turn execution path for new Codex workflows with scoped native tool sessions. This is a candidate improvement, not a claim that ORE beats standalone Codex on every task. The earlier frozen 54-run comparison remains unchanged. The new preregistered comparison includes a strong standalone baseline that can retain generated programs and execute them without another model call.

## Execution contract

A conversation defines the goal, access boundaries, outputs, budget and acceptance criteria. Unfamiliar work may start as a single agent node. It can call admitted tools, delegate bounded groups and resume the same parent session with their actual results or errors. Known work uses tool, recipe or foreach nodes without model calls. The approved graph need not predict every page or selector.

New runs use `ore.workflow/v2`; existing persisted v1 runs retain their old execution semantics. `mission.agent_runtime` accepts `auto`, `native` and `structured`. Codex auto execution selects the scoped native adapter. Other providers use the explicitly reported structured runtime until a native adapter is implemented; an explicit unavailable native request fails visibly.

The scoped Codex process enables the code tool host needed to compose ORE dynamic calls. Native shell, host filesystem, external MCP and independent browser tools remain disabled. Every ORE effect still passes capability validation, scope checks, task ownership/fencing and a durable operation receipt. Provider thread/turn/call IDs make replay identities stable. Unknown side effects require reconciliation. Stop is confirmed only after provider and tool execution settle; late usage/stop receipts cannot overwrite a newer worker's execution state.

A parent agent that delegates waits for a distinct child group and resumes afterward. A failed completion schema/check returns feedback in the same session, with at most two corrections. Repair is not allowed to bypass exhausted budgets, authentication or user intervention. An identical failure fingerprint is not automatically repaired again. Completion evidence references actual scoped receipts and artifact records; schema or receipt validation alone is not proof of corpus completeness.

## Context, accounting and progress

Initial supplied workflow context is limited to 64 KiB; continuations contain bounded deltas. Larger values remain available through paginated inspection. The planner similarly retains large source/context values behind conversation-scoped read handles. Public chat output contains operational summaries and results, never hidden reasoning.

`RunBudget` persists planning, execution and repair usage under one scope. Cumulative provider counters are charged as monotonic deltas, including native internal completions. Resume and replan preserve consumed time/tokens/received bytes. Confirmed human pauses exclude their waiting time; automatic downtime counts. In-flight usage can exceed the remaining allowance before the provider stops: the excess is recorded and prevents further effects. Missing accounting is not reported as zero usage. Budget increases require an approved envelope.

The chat/SDK expose actual execution runtime, selected model, time and token allowance, incomplete accounting, observed overshoot, recipe verification/reuse and validation strength. Existing collection progress and ETA remain separate from model usage.

## Verified deterministic programs

`ore.recipes.execute_recipe` interprets bounded JSON programs. It has no network, filesystem, shell or model access of its own. A host callback provides each admitted tool call with a stable operation ID. Programs support literal tools, references, pure expressions, bounded loops/conditions/retries, output checks and resumable checkpoints. The common primitive concurrency ceiling prevents nested loops from multiplying worker capacity.

```json
{
  "version": 1,
  "steps": [
    {"id": "page", "tool": "fetch", "inputs": {"url": {"$ref": "inputs.url"}}},
    {"id": "text", "tool": "content.select", "inputs": {
      "html": {"$ref": "steps.page.html"}, "selector": "article p", "text_mode": "raw"
    }}
  ],
  "output": {"$ref": "steps.text"}
}
```

An explicit candidate can run under the approved task scope. Automatic reuse requires five distinct inputs checked by a registered host verifier. The verifier identifies actual observed cases so model-supplied nonce changes do not create fake validation cases. Tool implementations, source contract, access policy and verifier version form the reuse contract. Changed contracts or failed independent checks prevent reuse. Host verifiers are installed code, not model-writable success assertions.

Workflow native tools expose `recipe.propose`, `recipe.inspect` and `recipe.execute`. A promoted replay hint can execute before any model call; a stale or failing hint returns durable receipt context to the native agent for repair. Only affected work falls back. Model confidence, matching JSON shape and successful HTTP status do not promote a recipe. Native model routing retains the quality baseline unless independently validated full-outcome evidence supports a cheaper route; old single-action calibration cannot authorize v2 downgrades.

## Finite release gates

The new harness separates preregistration, runtime freeze and execution:

```sh
uv run python scripts/benchmark_architecture_v2.py preregister --directory .ore/reports/comparison-v05
uv run python scripts/benchmark_architecture_v2.py freeze --directory .ore/reports/comparison-v05 --confirm-runtime-frozen
uv run python scripts/benchmark_architecture_v2.py run --directory .ore/reports/comparison-v05
```

It runs 36 old regression arms, 24 new held-out arms and 12 warm/changed follow-ups, sequentially in paired order. Both arms receive the same primitives and outer checks, Astra/high and concurrency five. New larger cases contain 100–1000 source items. The three repeated families include cold preparation, verification, unchanged-layout fresh data and changed-layout repair in total wall time and total tokens. Both totals must improve by at least 20%, each chosen family must avoid regression, and baseline-success/ORE-failure is prohibited. Missing usage or a changed frozen runtime fails the gate. The held-out seed and expected output are never provided to the actor.

Journal acceptance separately requires an independently verified official TOC, original-article classification, main PDF identity/version, every declared supplementary file, and saved byte/hash checks for JACC 83(1), EHJ 45(1), Circulation 149(1), and JAMA Cardiology 9(1), all from 2024. Resolver candidates, a browser opening, prior sample files and synthetic fixtures cannot satisfy this gate. Pending APIs stay excluded. External access gaps remain visible.

Local package artifacts are built into `dist/v0.5/` with `scripts/build_release.py --output dist/v0.5`. Old `dist/` and frozen comparison evidence are retained. Nothing is published to a package registry. Final execution results and rollout status are recorded in `VALIDATION.md`.
