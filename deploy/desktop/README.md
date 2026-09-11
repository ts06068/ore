# Native Chrome desktop runtime

This image installs Google's ordinary stable Chrome package from Google's signed
APT repository. Xvfb and Openbox provide a private 1280x800 desktop. The fixed
`control.py` protocol receives JSON on stdin and operates native X11 mouse,
keyboard, clipboard, screenshots, and screenshot OCR. There is no Playwright,
CDP endpoint, extension, debugger, automation concealment, or sandbox-disabling
Chrome flag. A graphical container is not a full virtual machine: it shares the
host kernel. Installing Chrome this way does not guarantee any publisher or
Cloudflare will accept the environment.

Build from the repository root with `docker build -f deploy/desktop/Dockerfile
-t ore-desktop:0.2.0rc1 .`. The stable repository can change; record the resulting
image digest and installed Chrome version with each validated deployment.
The runtime never pulls images automatically.

`DesktopRuntime` is a host-side lifecycle component. The calling host needs
Docker access. The desktop receives only its private profile/download bind mount
and read-only Chrome policy file. It receives no Docker socket, coordinator
master secret, Codex credential, or provider API key. No desktop/X11 port is
published; frames and inputs go through fixed Docker exec commands. Credentials
entered in Chrome remain in that Chrome profile; the host profile directory is
private (0700), not encrypted by this runtime. The persistent profile/download
bind mount has no filesystem quota imposed by this module; production disk
ceilings require a quota-managed host path. Reopening the same session ID
reuses the native profile. A profile_id is metadata; different session IDs do not
automatically share or import browser credentials.

The caller must supply exact permitted HTTP(S) origins. Chrome managed
URLBlocklist/URLAllowlist constrain navigation, including iframe navigation.
Support-only origins are not promoted automatically to navigation capabilities.
These browser policies do not provide full request-level egress filtering,
per-origin rate enforcement, or the same interception guarantees as the older
Playwright backend. Production egress restrictions require an external network
policy. Desktop input has a finite allowlist of keys and coordinate bounds;
OS launchers and developer-tools shortcuts are not available.

The default `resource_mode='cgroup'` requires Docker CPU, memory, and PID hard
limits. It does not silently fall back on unsupported hosts. An explicitly
selected diagnostic `resource_mode='watchdog'` retains a hard PID ceiling and
uses the root threaded cgroup parent, one session per host user, monitored
aggregate process RSS and cgroup CPU time, and at most 300 seconds lifetime.
RSS and CPU budgets are sampled and may overshoot between checks; they are not
hard memory or CPU quotas. Missing accounting terminates the diagnostic session.
Production resource readiness is not established by watchdog-mode success.

All containers keep a non-root user, read-only root filesystem, no-new-privileges,
cap-drop ALL, bounded /tmp and /dev/shm, and Chrome's own sandbox. Some Docker
hosts additionally require user-namespace support. `seccomp-chrome.json` derives
from Moby's default deny-by-default profile retrieved 2026-09-11:
https://github.com/moby/profiles/blob/main/seccomp/default.json
Upstream base SHA-256:
536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74
The only added rules allow:

- unshare with exactly CLONE_NEWUSER (0x10000000);
- clone with exactly CLONE_NEWUSER|SIGCHLD (0x10000011);
- clone with exactly CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET|SIGCHLD (0x70000011);
- chroot, used by Chrome inside its new user namespace;
- clone with exactly CLONE_NEWPID|SIGCHLD (0x20000011), used by
  Chromium NamespaceSandbox::ForkInNewPidNamespace for sandboxed child processes.
  Primary source: https://github.com/chromium/chromium/blob/main/sandbox/linux/services/namespace_sandbox.cc

No host capabilities are added, and kernel capability checks still apply.
The upstream profile is Apache-2.0 licensed; its license is in LICENSE.moby.

Download receipts query only Chrome History's downloads/downloads_url_chains in
an SQLite read-only transaction. A receipt requires a completed record, HTTP(S)
URL chain, exact received/total size, a confined regular file, and a SHA-256 hash.
Incomplete files and symlinks are ignored. Such a receipt proves observed browser
download provenance; Vault must still verify format, identity, version, and
supplement association. There is no fabricated HTTP status, DOM, or HTML source.
