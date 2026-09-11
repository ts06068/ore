# Chrome on a separate PC

ORE's Chrome companion connects one dedicated tab in the user's existing Chrome profile to one mission. The LLM still runs on the ORE coordinator. Navigation and session-bound file requests run on the Chrome PC, so the browser's existing login and network configuration remain there. Cookies and browser-profile databases are not exported to ORE.

This is an additional browser transport, not a demonstrated Cloudflare bypass. Chrome shows its normal debugging banner. The implementation uses the public `chrome.debugger` API and fixed operations, with no browser-identity patch, solver service or clearance-token injection. Existing challenge attempt budgets and target-recovery verification still apply. A working ordinary tab is not proof that an extension-controlled tab will be accepted by the publisher.

## Install and connect

1. Open the selected ORE handoff in the Chrome that can access the journal. Expand **Use Chrome on this PC** and select **Download Chrome companion**.
2. Unzip the download. Open `chrome://extensions`, turn on **Developer mode**, select **Load unpacked**, and choose the folder containing `manifest.json`.
3. In ORE, select **Create pairing code**. Open the extension popup, paste that code, and select **Connect dedicated tab**. Chrome creates a new tab; the extension does not attach other existing tabs.
4. When ORE shows **Chrome connected**, select **Use connected Chrome for this request**. The existing handoff now points to the dedicated Chrome tab. Complete any required login in that tab and use **Verify & resume** only after the requested journal page opens.

The code expires after five minutes, is single-use, and authorizes only the selected mission revision. The coordinator retains its hash, not the token. The extension does not persist the pairing token. Disconnect in the extension to stop control; an unexpected disconnection pauses the mission and creates a new recoverable request. A coordinator restart requires pairing again.

Use the server address already shown in the ORE browser window. A remote address requires HTTPS/WSS. `http://127.0.0.1:8765` is accepted when an SSH/local port forward on the Chrome PC already reaches ORE. Reverse proxies must forward `/v1/companion/socket` as a WebSocket. No inbound listener is needed on the Chrome PC, and no public remote-debugging port is opened.

## Collection behavior and limits

- The current integration works with the local coordinator backend. Existing isolated executor containers remain a separate execution mode.
- Browser state reports `transport=chrome_companion`. Agents must reuse that session and include `session_id` on `download` calls to obtain bytes from the Chrome PC. Ordinary server API requests do not inherit that browser's login.
- Pairing and manual access verification do not change an API-only mission into an automatic browser mission. If `browser_fallback=false` remains configured, automatic browser tools stay disabled. Enable browser fallback in the applicable mission/profile when browser automation is intended; API-first ordering can remain selected.
- Navigation and returned content are checked against explicitly paired mission origins. Normal Chrome page dependencies use Chrome's own networking; this transport does not provide the network isolation or per-subresource admission of ORE's managed Playwright executors.
- File requests run as a bounded browser `fetch` in an isolated JavaScript world. The maximum is 64 MiB per file, or the mission limit when smaller. Redirects and CORS failures are reported as gaps. Browser-native downloads triggered directly by a page are not imported automatically; use observed URLs with ORE's `download` tool.
- Original file chunks return to ORE for its existing identity, format, hash and coverage validation. A received file is not automatically an eligible original article or a complete supplement set.
- DOM captures remove scripts/form values and hidden password inputs; visible authentication forms are rejected. Captures are labeled `rendered_dom_companion_sanitized`. Only an observed top-frame HTTP response supplies status; an unknown status cannot seal an official manifest.
- Screenshots cover at most the top-left 1280 × 800 CSS pixels and preserve coordinate mapping on high-density displays. Resize the Chrome window if a needed control lies outside that region. Login secrets should be entered directly in Chrome.
- Connections expire after one hour. Mission revision changes revoke the connection, and a lost companion never silently changes to a server browser.

## Validation boundary

The local integration test loads the actual unpacked extension into Chromium and pairs it over the real authenticated WebSocket. It exercises dedicated-tab attachment, DOM/screenshot observation, an ordinary fixture checkbox with the existing challenge budget, an HTTP-status-backed snapshot, and a file requiring a Chrome-only HttpOnly cookie. It verifies byte equality, absence of exported browser credentials, disconnection recovery and prevention of server-browser fallback.

That test uses a controlled local website and programmatic browser actions; it is not a production publisher test, not a new LLM run, and not execution on the user's separate PC. Live JACC/EHJ/Circulation/JAMA acceptance remains pending client installation and verification. Current regression and package evidence belongs in [VALIDATION](../VALIDATION.md).

Chrome references: [debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger), [WebSockets in extension service workers](https://developer.chrome.com/docs/extensions/how-to/web-platform/websockets).
