# Browser repair and production challenge boundary — 2026-09-11

**Historical Playwright diagnosis.** The later ORE-owned native Chrome implementation and current validation are documented in [desktop-chrome.md](desktop-chrome.md) and [VALIDATION.md](../VALIDATION.md). The observations below describe the earlier transport.

A fresh, unchanged Playwright Chromium 151.0.7922.34 visited the official compatibility page without ORE routing or operator cookies. It returned HTTP 200 and explicitly displayed `Automated Browser Detected`; `navigator.webdriver` was true. No checkbox was clicked. This isolates the compatibility finding from ORE's origin filter and persisted institutional session, but does not prove the precise reason for any publisher's challenge decision. Evidence: `.ore/reports/cloudflare-diagnosis-current.json`, screenshot of the same name, and `cloudflare-diagnosis-classified.json`.

Cloudflare's [supported-browser documentation](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/) does not support automated browsers including Playwright for production challenges. An LLM choosing a checkbox interaction does not itself change the browser's compatibility. Headed mode alone has not been verified as a remedy here.

## Corrected network diagnosis

The fresh trace contains two failed DNS probes on strict challenge subdomains and two HTTP 401 responses on recognized private-access-token paths. Cloudflare [documents these cases as expected](https://developers.cloudflare.com/cloudflare-challenges/troubleshooting/challenge-solve-issues/). Those observations alone do not establish a broken connection or failed challenge.

The previous statement that `brunhild.challenges.cloudflare.com` was a missing required dependency causing the loop was unsupported. Its initial ORE scope denial was real, but the subsequent name-resolution failure is consistent with an expected probe. This is separate from the earlier verified denial of the apex `challenges.cloudflare.com` support script. Apex DNS failures, unapproved origins, timeouts, unrelated 401 responses and other HTTP errors remain actionable.

## Implementation

- Admitted DNS failures now abort with the browser's native name-resolution error instead of a misleading client-policy-block error. Expected probe failures are retained as informational observations; this does not grant new network access.
- Network and runtime findings are classified separately. A true webdriver flag is an automation signal, not proof of a publisher rejection. An explicit official compatibility-page finding has its own code. Diagnostic evidence omits opaque URL credentials and token-bearing paths.
- Concurrent browser-profile writes merge cookie and local-storage changes against each context's baseline under the encrypted store transaction. Disjoint edits survive, intentional deletions remain deleted, and stale conflicting writes keep the current stored value with a counts-only conflict report. This applies across executor RPC as well as local contexts. It is not evidence that a profile conflict caused the current Cloudflare loop.
- Diagnostic event tasks are tracked and drained at session close; a failing log sink cannot leave a denied request pending. Failure metadata excludes exception bodies.
- Recovery checks consider visible password fields. A hidden publisher login dialog no longer falsely invalidates accessible content; a visible authentication gate still prevents successful recovery.

Challenge histories and exhausted budgets are preserved. There was no production challenge attempt or budget reset during this repair. The institutional API/open-access preference remains the configured default.

## Remaining integration requirement

The operator reports that a normal browser opens the journals. Its location and a supported way for ORE to work with that browser have not yet been verified. The current process has no configured desktop display; an existing X11 socket alone does not establish an accessible user session. No ordinary browser has been attached or transferred into ORE. A supported publisher/API route remains usable where entitled, while direct journal automation is still an unmet acceptance condition.

The four original issue inventories and exhaustive publisher supplement coverage remain unverified. See [the real journal test](real-journal-test-2024.md) for the previously retained sample files and their limits. Current regression and package evidence is recorded in [VALIDATION](../VALIDATION.md).
