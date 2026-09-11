# Connection and desktop flow in 0.6

Connection cards are stored independently of collection jobs and may belong to a conversation. Global model connections remain visible after a conversation is created. Logging in, storing a key and opening an operator browser do not require a model invocation.

## Operator API

All routes require the existing operator authentication and mutation origin checks.

| Route | Contract |
|---|---|
| `GET /v1/connections?conversation_id=...` | Current cards, including global model cards. Polling refreshes an owned pending CLI login without starting another login. |
| `POST /v1/connections` | `{provider, conversation_id?, access_profile_ref?, operation?}`. Reuses the existing matching card. |
| `POST /v1/connections/{id}/actions` | `{action, expected_version, idempotency_key, agent_backend?, agent_model?}`. Supports `start_login`, `refresh`, `cancel`, `open_provider`, `mark_pending`, `request_agent` as appropriate. |
| `POST /v1/connections/{id}/secret` | `{field, value, expected_version, idempotency_key}`. Source fields: username/password/API key. API model fields: API key only. Returns references and field names, never values. |
| `POST /v1/connections/{id}/login-code` | Protected, transient official Claude CLI completion code. The code is not stored in cards, receipts or messages. |
| `GET /v1/providers/status?include_usage=false` | Separate provider account status/quota and source credential-group quota. Unknown or unsupported limits remain unknown. |
| `GET /v1/desktop/preview?handoff_id=...` | Validates current handoff, profile, finite origins, DNS/access policy and checkpoint. Returns eligibility, exact origins, scope basis and expected version; creates no browser. |
| `POST /v1/desktop/attach` | Existing `{handoff_id, expected_version, idempotency_key}` contract. |

Card `kind` is `model` or `source`. Subscription providers are `codex` and `claude_code`. API providers `openai` and `anthropic` expose `backend: {kind, api_key_ref}` after protected storage; their state is `configured_unverified`, because storing a key does not verify entitlement or quota. API model IDs remain an explicit user selection.

Actions use version checks and durable idempotency receipts. Interrupted effects are marked for review after restart, rather than automatically repeated. Source setup agent handoffs are resolved from the actual agent job, so an authentication request remains visible in its connection card. A stop without confirmation stays `cancel_unconfirmed`.

## Official subscription login

Codex uses the installed app-server's `account/login/start` device-code flow and owned `account/login/cancel`. Existing accounts are checked first. Account read, rate-limit read/notifications and optional safe token-activity summaries are separate from task budget accounting. Account identity and auth notifications do not enter public model streams. See the [official app-server documentation](https://learn.chatgpt.com/docs/app-server).

Claude Code uses its official CLI through the optional SDK integration. Authentication URLs/codes exist only in memory while pending; account tokens remain under the official CLI. These paths were tested with protocol/process fixtures; no real account enrollment or login was performed for this change.

## Source setup and finite scope

New setup and ad-hoc browser jobs persist their starting HTTP(S) origin. Source registry `docs` URLs are recognized when no dedicated provider entry exists. Legacy taskless manual handoffs may attach using only their already persisted checkpoint origin; the collection mission and challenge records are unchanged. Workflow tasks and collection missions cannot use this compatibility path to acquire scope.

Optional setup assistance creates a regular v2 workflow using Codex or Claude Code. Host enforcement allows scoped navigation, observation, scrolling, waiting and handoff; form click/type/key actions and other network tools are denied. The operator handles authentication, MFA, form mutation, registration submission and terms in authenticated browser controls. A newly encountered OAuth origin needs an explicit finite scope decision. This implementation does not claim fully automatic account registration.

Pending API approval is recorded for the selected operation and excluded from source admission. Main collection continues only through other available, authorized operations.

## Validation

Final focused regression command:

```sh
python -m pytest -q tests/test_connections.py tests/test_provider_auth.py tests/test_desktop_api.py tests/test_handoffs.py tests/test_server.py tests/test_agent_sessions.py tests/test_public_stream.py
```

Result: **133 passed**, 43.13 seconds. Tests cover private auth notification handling, unsafe login URL rejection, cancellation acknowledgement, quota sparse updates, protected-input error redaction, at-most-once effects, restart review, global/conversation card linkage, API key references, delayed CLI login polling, setup mutation denial before dispatch, ad-hoc origin persistence, and legacy desktop attachment without changing mission/challenge state. Main service and existing job database were not restarted or edited for these tests.
