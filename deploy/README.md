# ORE isolated execution deployment

> 0.3 chat workflows currently run on a coordinator with local workers and a configured Codex/API/local-model backend. The legacy Compose topology below sets `ORE_MAX_WORKERS=0` and uses a host model worker; it does not dispatch 0.3 workflow tasks. For the verified chat deployment use `ore serve --workers 3` on the Codex-authenticated host; browser/network executors can remain remote. See [0.3 deployment boundaries](../docs/release-0.3.md).

The `0.2.0rc1` topology separates the coordinator, two browser/network executor containers, a document validator, and PostgreSQL. A host-side Codex worker supplies model decisions using its existing local Codex login. The coordinator dispatches typed actions to executors; it does not run their browsers.

The release remains a candidate while the journal-specific acceptance gates are unresolved. The controlled container test described below is evidence of execution isolation, not a completed journal corpus or licensed access.

## Start the services

1. Copy `deploy/.env.example` to `deploy/.env` and restrict permissions with `chmod 600 deploy/.env`.
2. Set four independent random values: `ORE_AUTH_TOKEN`, `ORE_POSTGRES_PASSWORD`, `ORE_EXECUTOR_ENROLLMENT_TOKEN`, and `ORE_VALIDATOR_TOKEN`. Generate each with `python3 -c 'import secrets; print(secrets.token_hex(32))'`. Keep the file outside source control.
3. Build and start:

   ```sh
   docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
   docker compose --env-file deploy/.env -f deploy/compose.yaml ps
   ```

4. Open `http://127.0.0.1:8765` and authenticate with the operator token.
5. On the host where Codex is already signed in, set `ORE_AUTH_TOKEN` for that process and run:

   ```sh
   codex login status
   ore worker --server http://127.0.0.1:8765 --parallel 2
   ```

`ORE_EXECUTOR_REPLICAS=2` creates two execution containers by default. Increase it explicitly when more independent browser environments are needed. Model-worker parallelism, available executors, task limits, and access-profile session limits all constrain actual concurrency. A human-held browser continues occupying its executor.

Each executor is capped at 2 CPU cores, 4 GiB memory, 512 processes, a 2 GiB temporary filesystem and 1 GiB shared memory. The validator has 2 cores, 2 GiB memory, 128 processes and a 1 GiB temporary filesystem with execution disabled. The coordinator has 2 cores, 4 GiB memory and 256 processes; PostgreSQL has 2 cores, 2 GiB memory and 256 processes. Swap is disabled for these containers. Temporary files and shared memory count against the memory cap, so their configured sizes are ceilings, not extra available memory. With two executors the memory caps total 16 GiB, excluding host services. Adjust these explicit Compose limits to the deployment's capacity before increasing replicas. A parser or browser exceeding its cap can lose its process and require retry or handoff; these limits do not make hostile documents safe.

The coordinator listens on loopback through the published port; PostgreSQL, executors and validator publish no host ports. Use HTTPS or an SSH tunnel for access from elsewhere. The executor channel uses outbound authenticated HTTP polling and uploads; individual Chromium/CDP ports remain private.

## Roles and credentials

| Role | Responsibilities | Persistent access |
| --- | --- | --- |
| Coordinator | Mission/Rune, task fences, source policy, budgets, evidence ledger, encrypted profiles, central Vault and UI | PostgreSQL and coordinator state volume |
| Executor | Chromium, source HTTP/API access, downloads and browser control | Private tmpfs state; scoped coordinator assignment capability |
| Validator | PDF/ZIP/Office verification, extraction and OCR when installed | Private tmpfs; dedicated validator token; internal-only network |
| Host Codex worker | Actual model turns and typed decisions | Existing host Codex authentication |

Executors receive only credentials referenced by their assigned access profile or explicitly authorized operator action. They do not receive the coordinator operator token, database password, master secret key, or Codex authentication files. The enrollment token permits executor registration; issued worker capabilities are bound to a worker ID, and assignment capabilities are bound to its boot identity and current task attempt.

The validator runs each document in a child process with a clean environment and a 120-second parsing deadline. It terminates the process group and waits for the child to exit before accepting the next document. Input is limited to 256 MiB, derived bytes to 64 MiB, and the serialized response to 128 MiB. The validator receives neither publisher credentials nor executor enrollment credentials. `ORE_VALIDATOR_URL` and its dedicated token configure both coordinator and executor Vault facades to send document parsing to that service. The `ore-validation` Docker network is internal; the validator has no other network attachment or mounted host directory.

No service mounts the Docker socket or the host `.codex` directory. Do not add these mounts. The images contain no copied Codex credentials. The host worker's control-plane token is distinct from its Codex login.

## Sessions, files and recovery

An executor remains assigned to a task while its browser is live. Browser control uses a durable epoch in addition to task and assignment fences. A human handoff pauses the affected agent and keeps that exact browser available. Returning control and resuming the task rotates the assignment capability for the new task fence. Late results and uploads from the previous attempt are rejected.

A coordinator restart can reconnect to executors that remain running; the executor reports its sessions and frames again. If an executor process is lost, its live DOM is lost. Encrypted saved login state may seed a new session, but restored cookies are not a guarantee that a site's authentication remains valid. Operator handoff records retain the recovery context.

Browser page resources use a separate bounded pacing lane: `limits.browser_resource_min_interval_seconds` defaults to `0.1` seconds per origin, and an access profile may impose a larger minimum with the same field. Navigation, ordinary API/data requests and downloads retain their existing main-lane interval. Static assets, exact configured support-origin subrequests, and authorized same-origin `/cdn-cgi/challenge-platform/` bootstrap requests can use the resource lane only after normal URL/source/profile checks pass. Both lanes honor the same durable origin `429` cooldown. Existing main-lane reservations and floors are preserved; creating the resource lane does not reset them. Browser `network_diagnostics.request_grants` retains at most 100 redacted URL/type/lane/queue-time entries without bodies or cookies. This fixes ORE-imposed page-loading delay; it does not make an automated browser acceptable to a site that rejects automation.

Downloads remain original bytes. The executor uploads a receipt and the bytes; the coordinator checks byte count and SHA-256, invokes the isolated validator, and checks the task lease again before recording the artifact. HTTP/browser source pacing and upload budgets are stored centrally. Changing an executor or access-profile identifier does not create a fresh host rate bucket.

Institution access depends on the executor's actual network and approved proxy. A host Codex worker does not transfer its IP or login session to an executor. Containers on one Docker host can share an outbound IP; this topology does not establish anonymity or separate provider quotas.

## State and updates

`postgres-data` and `ore-state` persist the task database and collected corpus, encrypted profile snapshots and local encryption key. Back up them consistently and protect the backup as sensitive data. Executor and validator temporary state is disposable.

Use `docker compose --env-file deploy/.env -f deploy/compose.yaml down` to stop without deleting named volumes. Avoid `--volumes` unless intentionally deleting the stored database and corpus. Stop or finish active work before schema updates; follow the target release's migration instructions.

For a remote executor outside Compose, provision the same package and Chromium environment, then provide its enrollment capability and validator endpoint through a protected environment and run `ore executor --server https://COORDINATOR`. Its source requests must use the approved network zone. The private Compose validator endpoint is reachable only within its Docker network; a different deployment must explicitly provide an equivalently restricted route.

## Controlled acceptance

`tests/test_executor.py` exercises scoped authentication, cross-job and secret-access rejection, stale leases, original-byte upload checks, global rate floors, executor boot replacement and task-fence rebinding. These are fixture tests without model or account access.

The opt-in container test uses the locally installed `ore-engine:0.1.0` dependency image as a test base and overlays the current Python source:

```sh
DOCKER_BUILDKIT=0 docker build -f deploy/Dockerfile.executor-test -t ore-executor:test .
uv run python tests/executor_acceptance.py
```

It starts its own temporary coordinator, two actual executor containers and one validator; verifies two independent Chromium sessions, exact excerpt and download hashes, cookie separation, credential boundaries, retained human control, coordinator restart, and browser recreation after an executor process restart with resumption by the original task; and cleans up only its own test containers, networks and state volume. It makes zero model calls and uses only private synthetic source pages. The report is `.ore/reports/executor-container-acceptance.json`.

The fixture applies the deployment resource ceilings by default. A Docker host without usable cgroup controllers may fail before any service starts. For a functional fixture run only, `ORE_EXECUTOR_TEST_RESOURCE_LIMITS=false` records explicitly that CPU/memory/PID enforcement was not validated; it does not change Compose or establish production readiness.

Production builds use the root Dockerfile's `coordinator` and `executor` targets, not the test overlay image. Docker shares the host kernel; this deployment is not a full VM or a claim of complete production network isolation.
