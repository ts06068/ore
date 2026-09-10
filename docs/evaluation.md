# Paired routing evaluation

`ore.evaluation` compares **fixed Astra/high against automatic routing or an explicitly selected fixed experimental candidate inside the same ORE implementation**. Both arms receive the same mission content, exact Rune, finite reference set, source fixture identity and worker budget. It does not compare ORE with a plain agent, so its results cannot establish architectural superiority.

Unit tests use scripted model responses and temporary synthetic report envelopes. They provide contract coverage and cannot authorize a production model downgrade. Empirical calibration is a separate, explicitly invoked operation; its actual outcome is recorded in a report rather than inferred from passing tests.

## Runner

```python
from ore.config import Settings
from ore.evaluation import run_paired_evaluation

report = await run_paired_evaluation(
    Settings(state_dir=".ore"),
    cases,  # independently curated cases in the format below
    report_name="routing-classification.json",
    timeout_seconds=600,
)
```

Calling the default runner starts real Engine jobs and may invoke the user's configured Codex backend. Unit tests inject `engine_factory`; this forces `synthetic=true` and `evidence_kind=synthetic_fixture`. The test suite makes no external model calls.

Each case must declare these inputs before execution:

```json
{
  "fixture_id": "independently-reviewed-case-id",
  "fixture_sha256": "64 lowercase hex characters identifying the source fixture",
  "kind": "classify",
  "mission": {
    "goal": "Apply the agreed inclusion criteria to the specified records",
    "artifact_roles": [],
    "budget": {"max_agent_workers": 1, "max_turns": 20, "max_seconds": 120}
  },
  "rune": {"protocol_id": "reviewed.protocol", "protocol_version": "1", "instructions": "Exact reviewed instructions"},
  "expectation": {
    "included_resource_keys": ["canonical-reference-id"],
    "excluded_resource_keys": [],
    "artifacts": []
  },
  "source_state": {"mode": "fixture_replay"}
}
```

The example hash is explanatory, not a valid fixture. Actual expected artifacts declare `resource_key`, `role`, `sha256`, and optional `version`. Original bytes are hashed again during measurement; a stored `status=verified` flag alone does not pass. Resource keys follow the Store's canonical identity, usually normalized DOI, explicit ID or URL. Classification expectations include excluded records as well as included records.

A report covers one protocol digest and one task kind (`retrieve`, `extract`, or `classify`). Effective normalized missions, normalized Rune contracts, and independent expectations are persisted with their digests. Declared missions and their digests are retained separately because Engine.create merges Rune defaults before execution. Both measured effective mission digests must match the persisted effective reference. Source snapshots, access conditions and any source-state changes should be supplied in `source_state`; live-site drift is not controlled merely by assigning a fixture ID.

The runner alternates baseline/routed and routed/baseline ordering. Arms execute sequentially to avoid doubling traffic to a source. Each arm uses fresh isolated state under `state/evaluations/<evaluation-id>/case-<index>/<arm>`, preventing cross-arm caches or unfinished workers from contaminating the comparison. The same Engine code and settings are used, and a file fingerprint detects runtime edits during the experiment. Existing onboarding state and access profiles are not copied into these fresh engines. Public-source cases work with the default profile. An explicit `access_profile={"id": ..., ...}` is saved identically into each fresh engine through the normal secret-reference validation; only its digest is recorded in the report. This permits controlled local fixtures without copying institution cookies or onboarding secrets. Browser executable/library paths must be explicitly available to isolated engines.

A deterministic setup marks the automatically created planning task as setup-only and creates the requested bounded task kind in both arms. This setup is identical and uses no model. Thus these reports measure the declared task kind, not the quality or cost of an unmeasured planning phase. The runner retains execution time, measurement time, setup time, total wall time and each arm's job/state IDs. A timeout pauses and interrupts the actor; that arm fails quality.

## Independent quality and usage

`evaluate_job(engine, job_id, expectation)` computes:

- Final coordinator audit and unresolved gaps, plus task completion status.
- Missing and unexpected canonical records, including incorrect inclusion/exclusion.
- Expected versus observed artifact parent, role, version and SHA-256 as a multiset.
- Actual original-file SHA-256 compared with the vault manifest.
- Stable output-manifest and event digests.
- Observed model/effort/task-kind counts, operational failure-event count, and raw per-decision usage.

`quality_pass` requires the independent finite expectations, verified original artifacts, completed job/tasks, and an audit with no gaps. Global search recall remains unknown. Counts of tool/agent failure events are operational indicators, not estimates of a population error rate.

Usage is aggregated only from observed event values. Missing input/output/total-token fields remain `null`, with partial totals and missing-field counts retained. Human intervention duration and total HTTP request count remain unknown where the runtime does not measure them. The implementation does not turn model labels into token prices.

**Token-price or monetary savings are unproven without complete measured usage and an applicable, dated price schedule.** A shorter run also does not prove lower total project cost: source changes, quotas, setup, fixture curation, protocol maintenance and human work can affect the comparison.

## Downgrade evidence gate

```python
from ore.evaluation import validate_routing_profile

result = validate_routing_profile(
    profile,
    state_dir=".ore",
    protocol_digest=current_job_protocol_digest,
    kind=current_task_kind,
    fixture_ids=reviewed_validation_fixture_ids,
)
# Only result["valid"] is allowed to enable the tested downgrade.
```

Profiles bind `report_ref`, `report_sha256`, `protocol_digest`, `kind`, the exact `fixture_ids`, `model`, and `effort`. `report_ref` is a JSON filename/path inside `state/reports`; traversal and escaping symlinks are rejected. `load_evaluation_report` verifies the exact file bytes against the supplied SHA-256 before parsing.

The gate requires all of the following:

1. An empirical report from this paired-comparison schema; synthetic runner output is rejected.
2. Matching report SHA-256, protocol digest, task kind and complete fixture-ID set.
3. At least five distinct paired cases, with distinct fixture/input pairs.
4. Identical semantic inputs and Rune in both arms, bound to persisted independent expectations.
5. Both arms pass the audit and exact output/reference invariants; artifact validation must agree.
6. Baseline observations actually used Astra/high, and **every routed case actually observed the claimed lower model and effort for the declared kind**.
7. Routed failure-event counts do not exceed the corresponding baseline in any case.
8. No runtime-code change during the paired experiment.

Accepted targets follow the current routing policy: retrieval → Sol/medium, classification/extraction → Terra/low. Plans and new-site reasoning are not certified for downgrade by this gate. Validation returns `valid`, rejection `reasons`, and a validated profile only when all checks pass; malformed evidence fails closed. An editable `passed: true` flag is not sufficient.

Automatic routing without prior valid evidence may select Astra in both arms. Such a comparison is recorded honestly and **cannot certify an untested Terra/Sol downgrade**. The optional `candidate_profile={"model": "gpt-5.6-terra", "effort": "low"}` provides that explicit, isolated bootstrap control for extraction/classification (retrieval uses Sol/medium). It compares fixed Astra with the fixed candidate, labels the report `fixed_astra_vs_experimental_profile_same_runtime` and the candidate policy `experimental_profile`, and uses ordinary fixed routing without manufacturing a passed production profile. The gate checks the declared candidate and every observed candidate model/effort. A passing experimental report can establish initial evidence only for its exact Rune, task kind and fixture set; it is not evidence that production auto routing chose the candidate. Reusing a validation report after source/protocol/runtime changes warrants renewed evaluation.

A local report hash establishes content binding, not provider-signed proof that a model ran. The operator must preserve trustworthy job/event/fixture provenance. Five passing cases meet this release's minimum gate; they are not a general statistical proof of equivalence or savings.

## Offline checks

```sh
.venv/bin/python -m pytest tests/test_evaluation.py -q
```

Tests cover scripted Engine pair execution, separate state and arm order, usage nullability, original-byte corruption, independent reference mismatch, report tampering and path escape, protocol/kind/fixture binding, insufficient cases, synthetic evidence, unobserved downgrade, higher failure counts, malformed reports and actor timeout/interrupt behavior. Synthetic acceptance-branch envelopes exist only in pytest temporary directories; no empirical production report or valid production routing profile is created by the tests.

## Explicit local calibration

```sh
.venv/bin/python scripts/calibrate_routing.py
```

This opt-in command performs real Codex model calls: five distinct locally served HTML paragraphs, each paired as Astra/high versus Terra/low for `kind=extract`. The English and Korean fixture bytes and independently predetermined exact excerpt SHA-256 values are retained in the report. Every arm must register one included resource, drive a real browser, save the exact `#target` excerpt, and pass the normal final coordinator audit. There are no institution accounts, publisher downloads, or challenge attempts in this experiment.

Arms run sequentially with alternating pair order, isolated state and a maximum ten decisions per arm. The script reuses only installed Chromium executable/libraries, never existing cookies or secret files. It writes `state/reports/routing-extract-calibration.json` and a separate `routing-extract-calibration-validation.json`; failed runs remain failures. It does not install the resulting routing profile or silently enable a production downgrade. A runtime source edit during the experiment invalidates equivalence, even if all output checks pass.

Five simple paragraph extractions are a narrow calibration set. They do not establish quality for arbitrary journal navigation, classification, PDF or supplement resolution, unfamiliar sites, or multi-agent architecture. They also do not demonstrate monetary savings, even when measured token counts or elapsed times differ.

The final calibrated runtime can also be checked through the production automatic policy:

```sh
.venv/bin/python scripts/calibrate_routing.py --report-name routing-extract-calibration-final.json
.venv/bin/python scripts/acceptance_routing.py --calibration-report routing-extract-calibration-final.json
```

The second command first requires the current runtime fingerprint to match the calibration. It creates a fresh isolated Engine, copies the exact report bytes into that Engine's report directory, and installs only the hash-bound local-fixture validation profile. It then runs one real `extract` task under `quality_constrained_auto`. Success requires observed Terra/low decisions and the exact independently expected excerpt hash through the normal audit. The resulting `routing-auto-acceptance.json` is separate from the fixed-candidate calibration; it proves automatic routing for that known fixture without relabeling the calibration as an automatic-policy run.

## Measured result: 2026-09-10

The final frozen experiment ran from **11:30:02 to 11:33:37 UTC**. All five distinct fixture pairs passed exact resource identity, excerpt SHA-256, file rehashing, task completion and final audit. Both arms had zero tool/agent failure events. Runtime fingerprints matched before and after; total experiment wall time was **215.46 seconds**, including setup and measurement.

| Arm | Passing cases | Model decisions | Input tokens | Output tokens | Total tokens | Execution seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed Astra/high | 5/5 | 20 | 247,829 | 1,583 | 249,412 | 118.29 |
| Fixed experimental Terra/low | 5/5 | 20 | 225,315 | 1,753 | 227,068 | 93.12 |

These token totals are actual Codex per-turn usage observations; cached and reasoning fields remain in the raw report. They are not monetary prices or an estimate of charges. Timing reflects this local fixture experiment and its cache/load conditions.

- Final report: [routing-extract-calibration-final.json](../.ore/reports/routing-extract-calibration-final.json), SHA-256 `0a985c87f262b23cea489ff900cb4fec90e2de4ba6944a94a1d8b4dfe3c0c60d`.
- Validated profile: [routing-extract-calibration-final-validation.json](../.ore/reports/routing-extract-calibration-final-validation.json).
- Protocol digest: `304f5052be011f1f67c4f3cb68165bab4d2dd89e27a2786f4f17463313967f2a`; kind `extract`; exactly `ore-exact-paragraph-v1-1` through `ore-exact-paragraph-v1-5`.
- Measured runtime digest: `67e62ddf326fb26d27d8bb4f296b45cab2feb019829b0ecb17fc74554a9927e0`.

The first report, `routing-extract-calibration.json`, is preserved. A subsequent validator hardening required artifact-byte measurement entries to match the output manifest; the final experiment was then repeated under the frozen final code. Both runs used the **same five distinct fixtures**, so repeated runs do not create ten independent validation cases.

The separate [routing-auto-acceptance.json](../.ore/reports/routing-auto-acceptance.json) passed in **17.56 seconds**. The production `quality_constrained_auto` policy actually selected **Terra/low for four decisions** and produced the exact expected excerpt through the normal Engine audit. Its runtime fingerprint matched the final calibration, and it used isolated state with the exact hash-bound report copy. This proves the downgrade path for one known fixture after validation; it does not broaden the calibration's task/site coverage.

No architecture-superiority or monetary-savings conclusion is supported by these runs. The five exact paragraph tasks do not validate unseen journal navigation, original-article classification, PDF identity, supplement discovery, or retrieval at archive scale. The operational validation profile remains tied to the recorded Rune, task kind and fixture IDs.
