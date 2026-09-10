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
