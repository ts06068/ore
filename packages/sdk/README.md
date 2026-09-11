# @ore/sdk

A dependency-free typed transport client for a running ORE server. This package does not start Python, call a model, or copy Codex authentication. Node 20+ or a modern browser with Fetch and streams is required.

```ts
import { OreClient } from '@ore/sdk';
const ore = new OreClient({ baseUrl: 'http://127.0.0.1:8765', token: process.env.ORE_TOKEN });
const jobs = await ore.listJobs();
for await (const event of ore.events(jobs[0].id, { after: '0' })) {
  console.log(event.event, event.data);
}
```

Use `createJob`, `action`, `resources`, `artifacts`, `exportJob`, `runes`, `validateRune`, `accessProfiles`, and `browserSessions` for the respective `/v1` APIs. `request<T>()` exposes additional versioned endpoints without removing their response fields.

`browserSocket(sessionId)` returns the WebSocket URL and authentication subprotocols. The server selects `ore.v1` and reads the `ore.token.<base64url>` token. Tokens are never placed in URLs. The client does not persist tokens. Applications should keep them in memory and authenticate their UI separately where appropriate.

`events()` is an abortable async iterator. Persist the returned `id` to reconnect using `after`. Reconnection is an application choice; incomplete SSE events are not yielded.

Development: `npm install`, `npm test`, `npm run pack:local`. The tarball is written to `artifacts/`; registry publishing is separate.

Version 0.2 adds durable operator handoffs and operation-specific source readiness:

```ts
const requests = await ore.handoffs(jobId);
const progress = await ore.progress(jobId);
const sources = await ore.sources('institution-profile');
// Display request.href as a durable link to the authenticated ORE console.
// An action uses the retrieved state version, control epoch, and a unique key.
await ore.handoffAction(requests[0], 'claim');
```

The server rejects stale versions/control epochs. Preserve an `idempotency_key`
when deliberately retrying the same action. Source readiness is scoped to an
access profile and operation; an issued credential does not prove entitlement.
`sourceSetup` creates a draft setup request. `sourceCheck` performs an explicit
bounded provider operation and may consume provider quota. Neither method
registers an account or accepts provider terms automatically.

## Multi-turn planning and interruptible runs

```ts
const chat = await ore.createConversation({ mode: 'plan', model_policy: 'auto' });
await ore.sendMessage(chat.id, { content: 'Collect the original JACC articles from June 2024.' });
for await (const event of ore.conversationEvents(chat.id, { after: '0' })) {
  // Display public messages/progress and persist event.id for reconnection.
}
// Fetch the latest durable plan and present it for user review before approval.
const snapshot = await ore.conversation(chat.id);
await ore.approvePlan(chat.id, snapshot.active_plan_id!);
await ore.interruptConversation(chat.id);
// Observe the confirmed paused state before offering Resume.
await ore.resumeConversation(chat.id);
```

`conversations()` lists chats. `sendMessage()` accepts Plan/Execute mode and
automatic/fixed model settings; Execute mode alone is not plan approval.
`conversationEvents()` accepts an AbortSignal and a durable `after` cursor,
transmitted both as an encoded query value and `Last-Event-ID`. It uses the same
credential handling and incremental SSE parser as job events. The iterator does
not reconnect automatically; the web console restores snapshots and reconnects.

Version 0.4 adds typed public message sequences, `CollectionProgress`, turn
correlation fields, and `conversationEventHistory(id, {before, limit})`. History
pages contain ascending durable events, `has_more`, and `before_cursor` for the
next older page. A `message.delta` has a stable `message_id`, a one-based
`sequence`, and public response text; `message.complete` carries authoritative
`content` and a terminal status. Restore snapshot messages before applying newer
deltas, deduplicate sequences, and do not infer content for missing sequences.
Collection denominators describe eligible items within the approved scope; they
do not establish global database recall or equate workflow completion to corpus
completeness.

`scheduler()` exposes the coordinator's observed global/job capacity. Keep
`desired_executors`, `ready_executors`, per-job `running`, and approved caps
separate. A browser/network executor count is not native Chrome readiness. An
uninitialized scheduler may have `global: null`; absence is not a zero count.
