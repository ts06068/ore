# ORE 0.6.0rc1: chat, connections and recovery

The JACC follow-up planner failure came from a descriptive `constraints.sources` string replacing a valid `mission.sources` list. The planner now retains that description separately, validates typed execution fields, and permits at most two targeted corrections. User-supplied invalid constraints remain errors. Failed turns preserve the previous plan and return a public error code and recovery instruction.

The exact two-turn JACC reproduction succeeded with actual Codex: 108.65 seconds for the original request and 108.15 seconds for the Scopus follow-up. Both produced reviewable plans, and the latter retained `sources=["scopus"]` and `official_first`. This Plan-mode check created no collection run and is not evidence that the articles were downloaded. Evidence: `.ore/v06-acceptance/planner-jacc-20260911T011437Z/planner-result.json`.

## Chat as the operating interface

New conversations default to Execute; Plan remains available. Automatic execution binds the plan to a host-created envelope containing the actual user request, selected access profile, source policy, provider, capabilities and budget. A model cannot approve its own scope, increase the budget, switch to a paid provider or introduce credentials. Changes requiring authority remain reviewable in the chat.

Folders support nesting, renaming, moving and deletion. Branching from a message copies the public conversation prefix and selected settings, with a new provider context. Active runs, approvals and credential values are not copied. Existing conversations retain their previous execution policy.

Connection cards are persisted with the conversation. Login and secret entry use separate protected controls rather than normal messages. Bootstrap does not require a working LLM. A source whose approval is pending is visibly unavailable and can be excluded from the current request; its pending application is retained.

Progress distinguishes discovered records, eligible resources, verified main files, supplements and unresolved inventory. It does not turn an unsealed search inventory into a completeness percentage. ETA requires measured samples; a quota wait displays the provider-observed next retry separately from completion ETA. SSE reconnect and refresh restore persisted state. The displayed version comes from `/healthz`.

## Providers and credentials

Codex uses its official local app-server login. Claude Code is an optional official SDK adapter:

```sh
python -m pip install 'ore-engine[claude]'
```

Claude's subscription configuration has an isolated private configuration directory. A subscription selection clears inherited API/cloud billing variables; it never silently switches to paid API billing. Explicit OpenAI and Anthropic API connections remain available through protected key references. An account's reported usage/limits are displayed separately from ORE's task budget; unsupported or unreported limits are shown as unknown.

Claude task-token observations currently use the SDK main-loop `usage` counters; these are per turn, while billing cost and `model_usage` are cumulative. Internal compaction/query accounting is not fully reconciled into the task ledger. See the [official SDK accounting contract](https://code.claude.com/docs/en/agent-sdk/cost-tracking#track-costs-in-streaming-input-mode).

The Claude adapter restricts the SDK to ORE's registered tools. Host-issued single-use tickets bind dispatch to the actual provider tool call ID. Provider sessions, effect receipts, token watermarks and confirmed interruption remain durable. The real installed SDK 0.2.152 accepted the configuration and completed initialization/shutdown without a model query. A subscribed Claude model conversation and OAuth completion have not been live verified on this machine.

Multiple preconfigured API credentials may be used only within their authorized allocation. By default, keys share a durable provider quota group. NCBI permits a single active configured key. Explicit independent allocations can be configured by the operator:

```json
{
  "sources": {
    "scopus": {
      "credential_pool": [
        {"id": "primary", "api_key_ref": "env:SCOPUS_PRIMARY"},
        {"id": "separate-license", "api_key_ref": "env:SCOPUS_SECONDARY",
         "independent_quota": true, "quota_group": "licensed-secondary"}
      ]
    }
  }
}
```

The same key referenced by two names still shares its health and quota state. HTTP 429, Retry-After and provider quota headers are persisted across workers and restarts. Ordinary throttling does not trigger key switching. Automatic retry waits are bounded to three per node, retain receipts and the original budget, and can be interrupted. Unknown reset times remain visible for user resolution. Creating accounts to evade usage limits is not part of this implementation.

Source setup assistance uses a finite provider-origin scope and can inspect the provider workflow. Authentication, registration submission, terms, payment and form mutation remain authenticated operator actions in this release. The current setup assistant is not a fully autonomous API enrollment service; receiving an application confirmation is not treated as an API entitlement.

## Desktop and source protocols

Provider setup and ad-hoc browser requests now persist explicit mission origins. Legacy checkpoint attachment derives only the approved exact origin, so a missing origin field no longer prevents the owned Chrome desktop from opening. Attachment does not resume the collection or clear challenge history. It does not establish that Cloudflare will accept the browser.

Scopus search accepts an explicit `query_mode="native"` for field syntax. Recognizable field syntax in plain mode is rejected before HTTP instead of being silently wrapped into an empty phrase search; authorized year/journal filters still apply.

KISS and RISS have versioned browser context packs, not invented search APIs. They require actual page observation, exact-field phrase evidence, original-research eligibility, database membership, pagination evidence, and verified main/supplement artifacts. Public/export metadata is never counted as downloaded full text.

## Verification and packaging

The live source outcome is documented in [the four-source report](release-0.6-sources.md); it includes both retained native failures and explicitly separate deterministic continuations.

Functional checks, live four-source attempts, package hashes and deployment receipts are recorded in `VALIDATION.md` and `.ore/v06-acceptance/`. Python wheels/sdists and the npm SDK tarball are local release artifacts under `dist/v0.6/`; no public registry publication is performed.

The prior 0.5 native comparison stopped after 40 of 72 planned cases. The partial 20/20 versus 20/20 result is not a completed comparison or evidence of superiority. Its reports, source snapshot and earlier journal failures remain unchanged. This functional release makes no new claim of superiority over standalone Codex or whole-issue collection completeness.
