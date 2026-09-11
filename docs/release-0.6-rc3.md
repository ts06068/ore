# ORE 0.6.0rc3: EHJ browser recovery

This release fixes a collection that stopped on an HTTP 403 while chat continued to show an unrelated planning browser challenge. Current execution requests now appear separately from planning history. Verifying a planning browser cannot start a collection job.

A confirmed fallback-capable HTTP 403 returns to the same native agent for another permitted route. The agent can select `browser_open({url, transport: "desktop_chrome"})` for ordinary Chrome with screenshot and coordinate input. Explicit access-profile requirements still apply. Native selection uses a session-only finite origin scope; the packaged EHJ context supplies its Cloudflare frame dependency without changing dates, article types, source policy or challenge budgets. Unsupported policy restrictions fail explicitly.

A resumed workflow task can reclaim its previous browser only within the same job, node, revision and access identity, after human control and pending approvals are resolved. A deployment that changes native tool schemas still requires a compatible plan repair; old provider sessions are never silently relabeled.

Automatic collection authority now includes ordinary `browser_action` input. Previously ORE could reserve a verification attempt while its own capability envelope rejected the subsequent click. Explicit tool restrictions still narrow permissions, and account/provider forms remain separately authorized. A regression exercises the host-created envelope through an actual reserved browser action and a completed workflow.

An expired shared challenge can be retried only through the authenticated operator request action. The prior episode is archived; one attempt and 120 seconds are allocated to that execution job, with adaptation disabled. If its native desktop was lost, ORE restores the original authorized checkpoint before starting the new clock. The agent has no tool for renewing this allocation, and a browser restart alone never resets it.

New native desktops receive a configurable 4 GiB memory budget. Chrome's optional local model component is disabled. Expected negative DNS probes no longer break diagnostic logging. Access verification requires the actual target page; known issue checkpoints also require the observed journal, volume and issue.

## Measured EHJ check

On 2026-09-11, a real Astra/high agent opened `https://academic.oup.com/eurheartj` in isolated ordinary Chrome. One reserved checkbox click was followed by the exact target URL, European Heart Journal and Oxford Academic page content, and a resolved challenge episode. The check took 53 seconds. It used no publisher credentials or production challenge resets. The preceding 2 GiB run stopped after 8.5 seconds at its observed memory threshold.

Evidence: `.ore/ehj-recovery/native/20260911T073915Z-72001a/reports/desktop-diagnosis.json`. This is page-access evidence, not proof that every June 2024 PDF and supplement has been collected. The tested host uses the explicitly configured single-session watchdog mode; its five-minute diagnostic lifetime remains bounded. Other networks and publisher decisions may differ.

Package versions are `ore-engine 0.6.0rc3`, console/SDK `0.6.0-rc.3`; the scholarly package remains `0.2.0rc3`. Public package registries have not been updated.
