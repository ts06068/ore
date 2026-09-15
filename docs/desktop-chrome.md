# ORE-owned Chrome desktop

`desktop_chrome` runs installed Google Chrome in an ORE-owned Linux container with an X11 desktop. ORE sends fixed mouse/keyboard operations and receives screenshots; it does not start Chrome with Playwright, CDP, a debugger extension, or an automation-hiding patch. The existing LLM decision loop receives those screenshots through ORE tools.

Each session has its own container, native Chrome profile, display and download directory. The web browser workspace shows the whole desktop, including Chrome's toolbar. Control epochs, challenge reservations and user handoffs still apply. Passive waiting does not consume an attempt; native verification actions allow up to 30 seconds of bounded recovery observation. This runtime does not guarantee that a publisher or Cloudflare will accept the browser.

## Build and select

The Python wheel includes the desktop build sources:

```sh
ore desktop-build
ORE_BROWSER_BACKEND=desktop_chrome ore doctor --browser
```

The supplied image targets Linux x86-64. Docker must be available on the execution host. The default image is `ore-desktop:0.2.0rc1`; `ORE_DESKTOP_IMAGE` selects a locally built image. A remote Docker daemon requires `ORE_DESKTOP_HOST_STATE_DIR` to name the daemon-visible equivalent of ORE's desktop state directory. Browser containers never receive the Docker socket or coordinator credentials.

An agent can request `browser_open({"url": "https://academic.oup.com/eurheartj", "transport": "desktop_chrome"})` when the access profile permits that transport. The requested URL is checked against the original policy before ORE derives a session-only finite scope. Native transport does not export cookies to HTTP clients.

Use `"browser_backend": "desktop_chrome"` on an access profile to select this runtime for its jobs. `ORE_BROWSER_BACKEND` sets the default. An unknown or unavailable backend fails explicitly; it does not fall back to Playwright. Existing companion pairing remains a separate option.

Native desktop sessions require a finite list of exact HTTP(S) origins in `mission.allowed_origins`. Include every permitted navigation/frame origin explicitly. Chrome's URL policy blocks iframes as well as top-level pages; it does not offer a frame-only exception. In particular, a Turnstile test may require `https://challenges.cloudflare.com` in this list. Adding it also allows Chrome navigation to that origin. Native selection exposes the finite admitted list in the session policy; configured support origins become explicit native navigation origins. For an authorized EHJ checkpoint, packaged context adds the known frame dependency and exact observed publisher asset origins (`https://oup.silverchair-cdn.com`, `https://watermark02.silverchair.com`) to the session only; collection scope and access-profile restrictions remain unchanged. See [Chromium's policy test](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/browser/policy/test/url_blocklist_policy_browsertest.cc), [Chrome URL filters](https://support.google.com/chrome/a/answer/9942583?hl=en), and [Turnstile frame requirements](https://developers.cloudflare.com/turnstile/reference/content-security-policy/).

## Existing handoffs in the console

Open the request under **Handoffs** and select **Use ORE Chrome desktop**. The authenticated local coordinator opens the original checkpoint, observes its URL and screenshot, and then swaps the browser on the same durable request. **Close virtual Chrome** frees the desktop so another request can use it. Opening or closing a desktop does not resume the mission or reset challenge attempts. For an expired current collection challenge, **Retry automatic verification** is a separate operator action: it archives the prior episode, restores a lost native desktop if needed, and grants one attempt within 120 seconds. The grant is restricted to that collection and cannot be renewed by the agent.

This explicit operator action uses a session-only copy of the mission/access profile. Configured support origins become explicit native navigation origins and are recorded as such. The historical `brunhild.challenges.cloudflare.com` negative DNS probe is omitted only when it came from support entries; an explicit primary origin is never dropped. The required `challenges.cloudflare.com` frame origin remains admitted. No global source preference is changed.

For the four known journal issue URL formats, access recovery requires the same checkpoint plus observed journal branding, volume and issue, with no challenge, authentication or error page. EHJ homepage/archive checkpoints use the installed journal context for branding; other pages require configured `desktop_success_text`. These checks do not seal article inventories. Native attachment in this console currently requires the local coordinator; the current host uses the explicit watchdog diagnostic mode, allowing one desktop at a time for at most five minutes.

## Evidence and downloads

- Observations are screenshots and approximate OCR, not original HTML. HTTP status is unknown. DOM selectors, `page_extract` and arbitrary JavaScript are unavailable.
- Recovery requires configured `desktop_success_text` markers, the observed target origin/path and absence of challenge/login/error markers. A checkbox click is not success evidence.
- A native download must have a new completed Chrome History receipt. Chrome holds an exclusive lock on the live database; ORE copies only History and its rollback/WAL files when their filesystem signatures remain unchanged, lets SQLite recover the private copy, and checks its integrity before querying download tables. Source files are never modified. ORE checks the observed redirect chain, file location, byte count and SHA-256 before staging it for existing format/identity validation. Click-triggered downloads and explicit `download(..., session_id=...)` share byte accounting.
- Browser cookies stay inside the isolated native profile. They are not exported to server HTTP requests. Native profile directories are private local browser state; they are not the encrypted Playwright storage-state format. Automatic profile migration/reuse between new sessions is not implemented.
- Screenshots/OCR cannot seal the existing systematic issue/article inventories. Complete collection still needs supported source snapshots and independently reconciled obligations.

The native URL policy provides navigation restrictions. It is not equivalent to Playwright's per-request interception, resource pacing or a network firewall. Unsupported path/API-operation distinctions and proxy overrides fail explicitly. Institution access must be available from the desktop's actual network/authentication context.

## Runtime checks

Default resource mode `cgroup` requires working Docker memory, CPU and PID controllers. Chrome runs as a non-root user with its own sandbox, a read-only container root, dropped capabilities and no new privileges. The supplied seccomp additions admit narrowly scoped namespace operations required by Chrome's sandbox; they do not disable that sandbox. Docker's init reaps exited clipboard helpers and OCR/image processing uses one thread to keep repeated observations within the PID budget.

The coordinator uses a 4 GiB memory budget for each new native desktop. Set `ORE_DESKTOP_MEMORY=6g` (or a positive `m`/`g` value) to change that budget before starting ORE. In `cgroup` mode this becomes the container's memory and combined memory/swap ceiling; in `watchdog` mode it is the observed process-memory stop threshold. Existing sessions retain their launch settings. ORE's managed Chrome policy disables Chrome's optional local foundation-model component, which is separate from ORE's configured LLM provider.

`watchdog` is an explicitly selected single-session diagnostic mode for hosts whose cgroup layout cannot enforce memory/CPU ceilings. Its PID limit remains enforced; process-memory, CPU-time and lifetime are monitored and the session is stopped at its diagnostic budget. This is not a hard kernel memory/CPU ceiling and is not selected automatically.

Run an anonymous journal compatibility check from a source checkout:

```sh
python scripts/diagnose_desktop.py --journal jacc
python scripts/diagnose_desktop.py --journal jacc --agent
```

`--agent` uses the configured Codex backend and one bounded challenge attempt. `--resource-mode watchdog` explicitly selects the diagnostic resource mode. Each invocation has separate state under `.ore/desktop-validation`, no account credentials, and no changes to existing challenge episodes. Reports distinguish native startup, target-page access, LLM decisions and file collection; a diagnostic page check does not claim full issue collection.

Cloudflare [documents automated environments as unsupported for production challenge solving](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/). The native transport must therefore be evaluated using actual target-page results, not a claim that an LLM or a Chrome installation guarantees acceptance.

## Measured journal access

On 2026-09-11, native Chrome opened JACC 83(1), Circulation 149(1), EHJ 45(1) and JAMA Cardiology 9(1), all from 2024. EHJ and JAMA each used an actual Astra/high decision loop and one reserved verification checkbox click, followed by the requested issue page and a resolved episode. JACC and Circulation opened without a challenge interaction in their anonymous checks. Exact evidence and the separate native download fixture are in [VALIDATION.md](../VALIDATION.md). This establishes the tested page-access path, not full-issue PDF/supplement coverage or acceptance on every network.
