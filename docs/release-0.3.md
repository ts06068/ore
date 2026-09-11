# ORE 0.3.0rc1

0.3 adds conversational planning, durable workflow execution and a chat-first console. Python is the coordinator/runtime; `@ore/sdk` is its TypeScript client. Both are locally packaged release candidates. No registry publication or complete four-journal corpus is implied by this release.

## Start and use

```sh
uv sync --locked --extra dev --extra scholarly
codex login status
uv run ore serve --workers 3
```

Open `http://127.0.0.1:8765` and authenticate with the local `.ore/operator.token`. The root page is chat; `/jobs` retains existing jobs. Start in Plan mode, describe the result, answer consequential questions, review the proposed scope/checks, then approve it. Execute mode alone does not approve the initial plan. The trusted “Apply changes and continue” control authorizes an explicit subsequent change; a page or model response cannot grant that permission.

The sidebar retains conversations. The main chat displays questions, plan revisions, selected models, public action summaries, checks, artifacts and intervention requests. It does not expose provider private reasoning. Stop interrupts planning and all active branches in that conversation. Status questions can be answered while work continues; a scope change pauses affected branches first. Resume preserves completed verified results.

```sh
ore chat --server http://127.0.0.1:8765
# Interactive commands: /plan /execute /approve /stop /resume /status
# /continue <change> explicitly applies a revision and continues.
ore chat list
ore chat status CONVERSATION_ID
```

CLI and MCP use the same authenticated conversation API as the console. Set `ORE_AUTH_TOKEN` for these clients. The SDK exposes `createConversation`, `sendMessage`, `approvePlan`, `interruptConversation`, `resumeConversation` and resumable `conversationEvents`.

```ts
const conversation = await ore.createConversation({ mode: 'plan' });
await ore.sendMessage(conversation.id, {
  content: 'Extract the first paragraph from https://example.com into paragraph.txt.',
});
for await (const event of ore.conversationEvents(conversation.id)) {
  console.log(event.event);
}
// Review the proposed plan before calling approvePlan(conversation.id, planId).
```

The graph coordinator needs `--workers` greater than zero. With the subscription Codex backend, that coordinator process must have a working local Codex login. The older `ore worker` remote model loop continues to run legacy missions; it does not claim workflow coordinator tasks. Docker HTTP/browser executors remain separate and can be scaled independently. API/local-model backends require their corresponding configuration; no paid-API fallback is silently enabled.

## From conversation to execution

`ConversationManager` retains messages, read-only reconnaissance, immutable plan revisions, approval records and public events. The planner sees actual capability schemas, Mission schema, installed site contexts, access profiles and source readiness. It compiles every proposed plan before displaying it. It can ask questions, inspect sources, answer status questions, pause affected nodes or propose a complete plan. An invalid graph or natural-language string masquerading as an executable check is rejected.

`WorkflowManager` executes five node types: `agent`, `tool`, `recipe`, `foreach`, and `condition`. Dependency references use JSON objects such as `{"$ref":"nodes.fetch.output.html"}`. Loops expand lazily. Only ready nodes are leased. Repeated tool and recipe nodes execute without LLM calls. Agent nodes can propose additional validated work under the approved envelope. There is no fixed journal-specific intent enumeration in this layer.

Every tool has versioned input/output JSON Schema, an implementation digest, a timeout, an execution location and an explicit replay-safety declaration. `ore.capabilities` Python entry points register installed extensions. Handlers must be asynchronous and must settle their own work on cancellation; synchronous handlers are rejected because cancelling an awaited thread does not stop the thread. Plugins are trusted installed code, not generated code. Their handlers must use the runtime's access and storage interfaces. Metadata declarations alone cannot sandbox a malicious installed Python plugin.

Built-ins include legacy retrieval/browser actions, `content.select`, `content.write`, `content.read`, `code.register`, `code.run`, and optional scholarly inventory tools. Exact text extraction uses `content.select` with `text_mode: raw`. Derived text/JSON/CSV files are distinguished from downloaded originals. Operation receipts include resolved input/dependency values and implementation digests. A retry does not repeat an unknown mutating side effect; it becomes `needs_reconciliation` unless the tool is explicitly replay-safe and the previous worker has settled.

## Adaptive models and validation

New or unvalidated work starts at the high-quality route supported by the actual backend catalog. Unsupported model/effort settings are rejected. Repeated deterministic tool nodes need no model. An agent family can automatically calibrate a cheaper supported model/effort through shadow decisions that never execute tools. Downgrade requires at least five independently checked paired outcomes and matching complete action sequences. Whole-output equality against approved input or a literal is eligible; schema shape, nonempty text and model confidence are not sufficient evidence.

Calibration is scoped to the task family, capability/runtime digests, instructions, scope and access policy. A failed check or changed contract invalidates the cheaper route and raises effort/model again. Shadow calls consume the shared turn/token budget. This is bounded empirical validation, not a proof that two models have equal quality on arbitrary content. The routing state machine is regression-tested with controlled backend outputs; live-model automatic savings have not yet been measured.

An implementation-only repair may replace a failing graph within the approved goal, scope, outputs, constraints, budget, acceptance and mission envelope. Acceptance cannot be weakened. Repair attempts are bounded. Changes to shared mission access/scope/budget pause the old run and require an approved replacement; unchanged verified artifacts can be rebound using their bytes, hashes, identity and provenance. Other graph changes recompute affected descendants, preserving independent completed branches.

## Stop, reconnect and estimates

A control epoch and task lease fence invalidate writes immediately on interruption. A paused local coroutine is not proof that a model, remote executor or generated-code container has stopped. The run retains separate worker, model and resource acknowledgements and stays `interrupting` when confirmation is absent. Resume retries pending termination checks before admitting replacement work. Remote cancellation receipts are authenticated without accepting late result payloads. A private boot-bound outbox retries lost responses; credentials from an old executor incarnation are never replayed as a new one. A separate control heartbeat can request cancellation during synchronous uploads. Physical stop may wait for the current socket operation (5-second I/O timeout) plus heartbeat latency; it remains unconfirmed until the operation actually settles.

Public SSE events have durable cursors for reconnect/reload. ETA uses observed durations only after enough comparable completed units exist; unknown expansion, pending access or insufficient samples remain explicitly unestimated. The UI's operational summaries describe observable actions and checks, not hidden model thought processes.

## Generated Python and JavaScript

`code.register` pins source, schemas and a local Docker image digest. `code.run` exchanges bounded JSON stdin/stdout with that program. Containers have no network, no provider/operator credentials, no coordinator filesystem mount, read-only source/root, dropped capabilities, no new privileges, a per-container PID limit, bounded output and a deadline. External retrieval and artifact commits stay separate checked workflow operations.

The default resource mode requires cgroup memory/CPU controllers. This workspace's Docker daemon lacks the required domain controllers. Its actual parallel acceptance used the explicit portable mode below; it was not a silent fallback:

```sh
export ORE_CODE_RESOURCE_MODE=rlimit
# Only if Docker runs outside this filesystem namespace:
export ORE_CODE_HOST_ROOT=/HOST/PATH/TO/STATE/code-staging
ore serve --workers 3
```

Portable mode uses per-process address-space/CPU/file limits and a container PID limit. It does not provide aggregate container memory/CPU-share enforcement. Use the default cgroup mode on a suitable host for that stronger resource boundary. Local images default to `ore-executor:0.2.0rc1` for Python and `node:24-alpine` for JavaScript; set `ORE_CODE_PYTHON_IMAGE` or `ORE_CODE_JAVASCRIPT_IMAGE` to prepared alternatives. Images must already exist locally and are pinned on registration. Generated code is never imported into the coordinator.

## Scholarly completeness

Official inventory and classification evidence are separate from file retrieval order. For “JACC June 2024”, the intended plan is official archive → exact issue dates → official original-research categories → identity cross-check/retrieval → main and supplement validation. Explicit user dates override bundled Rune demonstration defaults. `api_open_access_first` can prioritize permitted API/OA files without making an index the official denominator. Pending/excluded sources are omitted with recorded reasons.

`scholarly.archive_inventory` independently rehashes captured HTML and applies an installed trusted `html_archive` profile. It requires complete pagination/range coverage, accounted issue links/counts and exact issue dates. Profiles can be installed by the authenticated operator at `/v1/coverage-profiles`; model-generated claims cannot install trusted completeness evidence themselves. A completed node graph with incomplete domain coverage ends `finished_incomplete`.

Current tests exercise exact-month inventory checks against controlled archive fixtures. This release does not install verified production archive extractors for every date/layout of JACC, EHJ, Circulation or JAMA Cardiology. Those real issue inventories and complete supplementary manifests remain unverified in the retained journal acceptance record. Do not interpret the chat/UI/container tests as successful whole-issue collection.

## Verification and package artifacts

See [VALIDATION.md](../VALIDATION.md) for measured tests and local report paths. `scripts/acceptance_workflow_runtime.py` runs three real isolated Python programs in parallel, verifies their exact results and stored JSON artifact, and asserts zero model calls. Conversation acceptance used real subscription Codex for questions, reconnaissance and planning, followed by deterministic file generation. UI acceptance uses an explicitly supplied JSON fixture.

```sh
.venv/bin/pytest -q
# npm build/test in packages/sdk and web, or use the Docker-aware release script:
ORE_DOCKER_WORKSPACE=/HOST/PATH/TO/ore .venv/bin/python scripts/build_release.py
```

The release script builds wheels, source distributions, npm tarball and Chrome companion, verifies clean core-only/scholarly installs, SDK import, bundled UI and rebuild from the source archive, then writes `dist/release-manifest.json` and `dist/SHA256SUMS`. The scholarly adapter version remains 0.2.0rc1; the core and SDK are 0.3 release candidates.
