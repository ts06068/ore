# ORE deployment

This deployment runs one coordinator, its browser service, the static web console, and PostgreSQL. Codex stays on the operator's host. The image contains no Codex executable, ChatGPT credentials, publisher credentials, or operator token.

## Start the coordinator

1. Copy `deploy/.env.example` to `deploy/.env` and restrict its permissions (`chmod 600 deploy/.env`). Set `ORE_AUTH_TOKEN` and `ORE_POSTGRES_PASSWORD` to independent random values. Generate each with `python3 -c 'import secrets; print(secrets.token_hex(32))'`. Hex passwords are safe to interpolate in the database URL. Do not commit the environment file.
2. Build and start:

   ```sh
   docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
   ```

3. Open `http://127.0.0.1:8765` and enter the operator token. Check service health with:

   ```sh
   docker compose --env-file deploy/.env -f deploy/compose.yaml ps
   ```

The UI, `/v1` API and browser WebSocket use the same origin. HTTP login uses an HttpOnly, SameSite cookie. Use HTTPS when exposing the console beyond loopback. An SSH tunnel to the existing loopback port is sufficient for private remote access; the compose file does not publish PostgreSQL.

The build compiles the TypeScript SDK and React console, installs the Python packages, and uses the Playwright Python `v1.62.0-noble` image matching the required Python Playwright package. The runtime is non-root, has a read-only image filesystem, writable state/tmp volumes and a one-GB `/dev/shm`. These settings require browser smoke validation on the actual host; they are not a claim of complete production isolation.

## Run Codex on the authorized host

Install `ore-engine` and `ore-scholarly` on the machine where Codex is already signed in. Confirm `codex login status` there. Set `ORE_AUTH_TOKEN` in that process to the same ORE operator token, then run:

```sh
ore worker --server http://127.0.0.1:8765 --parallel 2
```

The token above is the ORE control-plane token, not a Codex token. Do not mount or copy `.codex`, `auth.json`, or browser login profiles into the image. Host workers use their existing Codex authentication and send tool decisions to the coordinator. `ORE_MAX_WORKERS=0` disables local LLM workers in the container. An API key and local model GPU are not required for this topology.

Multiple host agent workers are supported by the execution contract. Browser sessions and source requests are owned by the single coordinator in this deployment; this does not create a fleet of independent browser hosts. Account usage and source request budgets remain shared.

Subscription access depends on the coordinator/browser host's actual network and configured access profile. A remote Codex worker does not transfer its institutional IP or publisher session to the coordinator. For institution access, place the coordinator on the approved network or configure the approved proxy there.

## State, updates and stopping

`postgres-data` persists the task database; `ore-state` persists vault files, browser profiles and local encrypted secret storage. Back up both consistently, including the encryption key stored in the state volume. Protect backups as credentials and collected content may be present. Do not use `docker compose down --volumes` unless you intend to remove the corpus and state.

Use `docker compose --env-file deploy/.env -f deploy/compose.yaml down` to stop without deleting named volumes. Rebuild on updates. This initial version creates its schema; it does not yet provide version-to-version database migration scripts. Review the target release's migration instructions and back up both volumes before future schema changes.

`docker compose config --quiet` validates configuration only. An image build, database health, actual browser session, live Codex tool turn, and authorized journal retrieval are separate verification steps.
