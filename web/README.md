# ORE console

React/TypeScript console for the local ORE server. The default workspace is a conversation: describe a goal, answer planning questions, review the plan and run it. Existing mission authoring, job controls, resources/artifacts, browser takeover, Rune validation and institution access profiles remain available.

Build the sibling SDK first (`cd ../packages/sdk && npm install && npm run build`), then run `npm install && npm run build` here. Serve `dist/` using the ORE FastAPI server. Development: `npm run dev`; the Vite proxy forwards `/v1` to `http://127.0.0.1:8765`. Change `vite.config.ts` if your server uses another port. `npm test` runs the UI logic/component tests.

The console sends operator tokens as bearer headers, keeps them only in memory, and uses the server's HttpOnly login cookie for same-origin restoration. It does not persist tokens in localStorage, place tokens in URLs, or request Codex tokens. Browser WebSockets authenticate through subprotocols, with `ore.v1` selected by the server. A browser frame is controllable only while the server grants `control: human`; epoch and frame ID accompany every input.

Static assets are bundled without external fonts, image services, telemetry, or a CDN runtime dependency. An ORE server and a configured browser/Codex environment are required; building this UI alone does not verify live model execution or journal access.

The Python Playwright script `src/test/browser_smoke.py` runs against a disposable real ORE server with zero model workers. Set `ORE_UI_SMOKE_TOKEN` to that test server's operator token. It tests login, cookie restoration, mission creation/audit display, Rune save, authenticated browser frames and coordinate input, and mobile layout. It starts a local fixture on port 18999 and intentionally permits that fixture in a dedicated test access profile. Do not run it against a production corpus. Screenshots and a JSON result are written to `/tmp/ore-ui-smoke` by default.

The 0.2 console supports `/jobs/{id}` and `/handoffs/{id}` deep links through the
operator login. Requests survive coordinator restarts; the request screen can
restore a lost browser, claim the live session, or verify and resume. Source
setup is optional and starts with an existing account. Connections displays
stored evidence per source operation and offers protected key storage and an
explicit bounded operation check. A pending API can be excluded without
excluding the source's browser operation.

ETA remains unknown until the remaining workload is known and at least five
comparable completed samples exist for each remaining stage. Scholarly corpus
progress additionally requires an authoritative inventory. Provider approval and
human waiting time are not assigned a completion timestamp. The local Codex MCP
adapter returns these handoff links through status/polling tools; it does not
push unsolicited messages into a ChatGPT conversation.

## Conversation workspace

`/` opens a new chat; `/chat/{id}` restores a durable conversation after login.
The sidebar lists conversations. `/jobs` retains the mission list and legacy
structured mission form. Plan mode supports investigation and Q/A; Execute mode
does not approve an initial plan implicitly. Use **Approve and run** on the
reviewable plan card. Messages remain available while ORE is executing.

**Stop** requests interruption of planning and execution. The UI shows stopping
until the server confirms it; **Resume** becomes available for a paused
conversation. A message can discuss revisions without resuming a stopped run.
Public progress summaries, tool/model decisions, checks, collected files and
user requests appear alongside the conversation. Model choices and reasoning
options come from the connected runtime. Internal model reasoning is not a UI
data source.

Snapshots restore messages, questions, plan revisions and recent public events.
The authenticated SSE stream reconnects from its persisted cursor, deduplicates
event IDs and falls back to periodic snapshots while disconnected. No bearer
token is placed in the stream URL. ETA appears only when the server supplies a
measured range.

## 0.4 appearance and public streaming

The operating workspace uses a porcelain/graphite token palette. Light is the
default; the appearance switch stores only `ore.theme`. All existing mission,
source setup, browser and handoff routes use the same tokens. The original ORE
SVG is preserved in `public/brand/ore-original.svg`; compact and inverse variants
reuse its geometry. Inter and Noto Sans KR are pinned Fontsource dependencies,
bundled as local WOFF2 assets. Font licenses and provenance ship under
`public/fonts/`; the browser never requests a font CDN.

Plans, operational events and workflow progress stay attached to the user turn
that requested them. Older uncorrelated records are labelled Earlier activity.
Public `message.delta` events update the real saved assistant message using its
sequence; duplicate deltas are ignored, missing sequences wait for a snapshot,
and authoritative completion includes saved interrupted text. No simulated
character animation or private reasoning stream is used. Earlier operational
history loads in pages from `event-history` without exposing credentials.

Collection progress is separate from the number of known workflow nodes. An
eligible-item denominator must be sealed before the UI shows a completion bar.
Unknown attachment counts remain open, required and verified files remain
separate, and scope basis/unresolved reasons remain visible. The results panel
searches received artifacts without assuming every file is verified. Markdown
escapes source HTML and does not load remote images automatically.

`src/test/ui_v04_smoke.py --server URL --token-file PATH` exercises the built
app at 390/768/1024/1440 pixels, both themes, local logos/fonts, restored theme,
keyboard drawer controls and existing operational routes. It uses real operator
authentication and an explicitly labelled read-only design fixture for the rich
conversation. Its report does **not** claim live LLM streaming or external
journal collection; those require separate execution evidence.

The separate opt-in `src/test/ui_v04_stream_smoke.py --server URL --token-file
PATH` requests a public explanatory response from the configured real Codex
backend, stops after received text, checks exact partial restoration after
reload, and resumes to completion without creating a collection workflow. It
consumes real model capacity. The report records actual first-text timing and
keeps this evidence separate from the design fixture screenshots.
