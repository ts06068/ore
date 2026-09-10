# ORE console

React/TypeScript console for the local ORE server. The console covers mission authoring, job controls, live events, resources/artifacts, browser takeover, Rune validation and institution access profiles.

Build the sibling SDK first (`cd ../packages/sdk && npm install && npm run build`), then run `npm install && npm run build` here. Serve `dist/` using the ORE FastAPI server. Development: `npm run dev`; the Vite proxy forwards `/v1` to `http://127.0.0.1:8765`. Change `vite.config.ts` if your server uses another port. `npm test` runs the UI logic/component tests.

The console sends operator tokens as bearer headers, keeps them only in memory, and uses the server's HttpOnly login cookie for same-origin restoration. It does not persist tokens in localStorage, place tokens in URLs, or request Codex tokens. Browser WebSockets authenticate through subprotocols, with `ore.v1` selected by the server. A browser frame is controllable only while the server grants `control: human`; epoch and frame ID accompany every input.

Static assets are bundled without external fonts, image services, telemetry, or a CDN runtime dependency. An ORE server and a configured browser/Codex environment are required; building this UI alone does not verify live model execution or journal access.

The Python Playwright script `src/test/browser_smoke.py` runs against a disposable real ORE server with zero model workers. Set `ORE_UI_SMOKE_TOKEN` to that test server's operator token. It tests login, cookie restoration, mission creation/audit display, Rune save, authenticated browser frames and coordinate input, and mobile layout. It starts a local fixture on port 18999 and intentionally permits that fixture in a dedicated test access profile. Do not run it against a production corpus. Screenshots and a JSON result are written to `/tmp/ore-ui-smoke` by default.
