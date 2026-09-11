# Adaptive executor pool

New missions start with a soft concurrency target of 5. `budget.max_agent_workers`
is the plan-approved upper bound, not a fixed product-wide ceiling of 10.
`parallelism.initial` defaults to 5 and is clamped to that approved bound.
Existing explicit smaller budgets remain smaller. `parallelism.mode="fixed"`
disables feedback changes; `parallelism.per_origin` defaults to 2.

Every 15 seconds the scheduler observes runnable tasks, successful completions,
new failures, source cooldowns, and reported CPU/memory pressure. Three consecutive
windows with successful progress and queued demand permit an increase of one.
A new 429 cooldown, failure, or high resource pressure reduces the target. These
updates change durable scheduler control records without changing the mission
revision. SQL task claims atomically enforce the live target, approved budget,
global task ceiling, registered executor capacity, and known-origin concurrency.
Normal request pacing and shared challenge reservations remain independent limits.
Unknown/dynamic future URLs are still checked by source policy and request pacing;
the static task-origin admission hint is not a complete network interception layer.

`GET /v1/scheduler` requires operator authentication and reports desired, registered
ready, and pinned executor counts separately. A desired value is never proof that
containers exist. `foreach` replenishes completed iteration slots without waiting
for every item in the previous group. Explicit `batch_size` remains an upper bound.

## Host deployment

The coordinator/model runtime and host broker run on a trusted Docker host. The
broker accepts no arbitrary image, command, volume, or resource arguments from a
mission or model. Its image/network/resource configuration is fixed by its startup
arguments. It resolves the configured image to an immutable local image ID before
launching workers. A dedicated `ORE_POOL_TOKEN` authenticates only the pool report
endpoint. Workers receive one-use, 120-second enrollments bound to their worker ID
and network zone; they do not receive the pool token or coordinator operator token.

Build the release executor image and create the two networks once:

```sh
docker build --target executor -t ore-executor:0.4.0rc1 .
docker network create ore-execution
docker network create --internal ore-validation
```

Provision independent values for `ORE_POOL_TOKEN` and `ORE_VALIDATOR_TOKEN` in the
operator's protected environment or service manager. Do not place them in mission
text, command-line arguments, source control, or worker-mounted files. Run the
credential-free validator only on the internal validation network:

```sh
docker run -d --name ore-validator --network ore-validation \
  --read-only --user 10001:10001 --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 128 \
  --memory 2g --memory-swap 2g --cpus 2 \
  --tmpfs /tmp:rw,nosuid,nodev,size=1073741824 \
  --env ORE_VALIDATOR_TOKEN --entrypoint python3 \
  ore-executor:0.4.0rc1 -m ore.validation
```

The host coordinator needs its own validator URL using an address reachable from
the host. The executor uses the validator container name on the internal network.
For a coordinator on this same Docker host, obtain the execution bridge gateway
and validator address:

```sh
ORE_POOL_GATEWAY=$(docker network inspect ore-execution --format '{{(index .IPAM.Config 0).Gateway}}')
ORE_VALIDATOR_ADDRESS=$(docker inspect ore-validator --format '{{(index .NetworkSettings.Networks "ore-validation").IPAddress}}')
export ORE_EXECUTION_BACKEND=remote
export ORE_VALIDATOR_URL="http://${ORE_VALIDATOR_ADDRESS}:8770"
export ORE_SCHEDULER_GLOBAL_LIMIT=64
export ORE_POOL_MAX_EXECUTORS=64
ore serve --host "$ORE_POOL_GATEWAY" --port 8765 --workers 8
```

Here `--workers 8` is an **operator-selected local model/task-loop hard limit**,
not a recommended limit for every plan. Choose it and the global limits for the
host and account. New default `ORE_MAX_WORKERS` is 5; explicit 0 still disables
local task execution. Codex stays on this trusted host with its existing account.
The worker containers have no `.codex`, database, master secret, or Docker socket
mounts. For other network layouts, use a coordinator URL reachable from both the
broker and executor bridge; a container cannot use the host's `127.0.0.1`.

In a second protected service environment on the Docker host, run:

```sh
python -m ore.host_pool \
  --server "http://${ORE_POOL_GATEWAY}:8765" \
  --broker-id retrieval-host \
  --image ore-executor:0.4.0rc1 \
  --network ore-execution --validator-network ore-validation \
  --validator-url http://ore-validator:8770 \
  --hard-cap 8 --cpus 2 --memory-bytes 2147483648
```

The broker uses daemon CPU/memory observations, an allocation reserve, its own
hard cap, and coordinator demand. It starts no containers while there is no
runnable network work. A worker must register and keep heartbeating before it
contributes execution capacity. Startup failure does not turn desired capacity
into ready capacity. Retry has a cooldown; stale registration does not establish
that a disconnected process has physically stopped.

Workers are unprivileged, read-only, PID/CPU/memory limited, and use bounded tmpfs
for private downloads/browser state. They join execution and internal validation
networks. Only the validator role token is supplied for the validation service.
The validator itself has no public network membership or provider credentials.

Scale-down first marks an idle worker draining so it cannot take a new assignment.
It requires no open sessions, active assignments, or unconfirmed commands. Human
handoffs and active browser sessions remain pinned. Docker stop/removal is observed
before absence is reported; an unavailable Docker daemon yields `unknown`, not
`absent`. Idle worker grace defaults to `ORE_POOL_IDLE_SECONDS=120`.

## Explicit diagnostic resource mode

On this workstation, normal Docker cgroup CPU/memory limits are unavailable for
some container workloads. Production mode fails instead of silently removing them.
For bounded local fixtures only, the operator can explicitly select:

```sh
python -m ore.host_pool --server http://COORDINATOR:8765 \
  --network FIXTURE_NETWORK --image ore-executor:pool-test \
  --hard-cap 5 --resource-mode diagnostic_rlimit
```

This preserves non-root execution, read-only filesystems, capability dropping,
PID limits, per-process address-space/CPU-time limits, and bounded tmpfs. It does
**not** provide hard aggregate container memory/CPU ceilings. The mode appears in
broker reports. Omitting an isolated validator is allowed only in this diagnostic
mode; that does not verify credential-free document parsing.

`tests/pool_live_acceptance.py` performs an explicit five-executor HTTP fixture and
idle drain; it does not contact a publisher or model. Its result is stored at
`.ore/reports/host-pool-live.json`. This test does not establish native Chrome,
Cloudflare acceptance, document validation, or production cgroup compatibility.

## Native desktop and existing Compose deployments

This pool provisions the existing browser/network/download executor runtime.
Native Chrome has a separate host desktop lifecycle. Its diagnostic watchdog mode
has a shared **one-desktop limit**; five registered HTTP executors do not imply
five native desktops. Scheduler state exposes that limit and does not claim native
capacity was verified. Normal cgroup native parallelism requires a suitable host
and separate runtime acceptance. Active known browser sessions retain their
assignment; the pool does not reset challenge episodes or clone session identity.

The existing Compose file still starts two manual executors and sets coordinator
`ORE_MAX_WORKERS=0`. Those are compatible registered capacity, but they are not
managed by this host pool and are never drained by it. A zero-worker coordinator
also does not run workflow nodes itself. Use a trusted coordinator/model runtime
with a nonzero approved operator limit for the workflow path described above;
simply starting the host broker does not create a model worker or a Codex login.
