# Sandbox telemetry flow — LangSmith (+ Sentry) exporters (HTB-B4)

Status: **LangSmith leg VERIFIED end-to-end (live)** — the auth blocker was a
tenant-scoping gap, not a bad credential (resolution and evidence below); real
metadata-only ingestion into the sandbox project and SDK read-back both work
unattended. The Sentry leg remains credential-gated (no SENTRY_DSN). Nothing
in this flow is fabricated: every claim of "verified" below is backed by a
command and its real output.

## Contract pins (unchanged)

- Envelope schema: `docs/telemetry/correlation-envelope.schema.json` (sovav-hermes
  r-obs-00), version 1.0.0, sha256 `5271d5faa8bee8ad3d58885f4ab54f88d77af6e0620c78ba627665ca8433e70a`.
- Contract doc: `docs/telemetry/telemetry-contract.md`, sha256
  `599e38f3a403e1b39b67b0b8aa05daa7ce922890b602d53ff80fcdbbb55eb657`.
  Both hashes verified on this branch before implementation.
- Sink interface (pinned by B3, `transport.py`): `sink.deliver(events: list[dict]) ->
  {"ok": bool, "accepted": int, "detail": str}`. B4 exporters conform exactly.

## What was built

- `sinks.py` (repo root): `LangSmithExporter` and `SentryExporter`, both
  conforming to the pinned interface, zero new runtime dependencies
  (stdlib `urllib` only). `langsmith` SDK 0.13.0 is used ONLY on the read-back
  path (integration test), not as a runtime plugin dependency.
- Tenant scoping (LangSmith, resolved this lane): the exporter resolves a
  workspace id — constructor arg > `LANGSMITH_WORKSPACE_ID`/`LANGSMITH_TENANT_ID`
  env > runtime discovery via `GET /api/v1/workspaces` — and sends it as
  `X-Tenant-Id` on every write. The id is tenant metadata returned to any
  authenticated caller, not a secret. Discovery failures fail closed (no
  writes attempted), naming only the exception type.
- Metadata-only gate (contract §3.1/§3.3), structural not conventional:
  an exporter reads ONLY the pinned journal-record keys
  (`event_id`, `enqueued_at`, `attempt`, `envelope`); an event carrying any
  other top-level key is REJECTED (never forwarded, never silently stripped)
  and the batch stops there with the violation named in `detail`.
  Envelope payloads are forwarded VERBATIM into LangSmith `extra.metadata`
  (§1.6: `/` keys legal) and into Sentry tags (§1.6: `/` → `_`, `run_id`
  stringified) plus Sentry `extra.envelope`.
- Dedupe/idempotency: duplicate `event_id` counts accepted but is not
  re-transmitted; LangSmith run ids are deterministic per event
  (`langsmith_run_id_for_event`). Server-side, LangSmith keeps exactly ONE
  create per run id — re-POSTing an already-received run create returns
  409 "Run create payload already received" (verified live) — so a retried
  event's 409 is counted as accepted-duplicate, never a batch failure.
- Credentials (§3.4): read from env ONLY — `LANGSMITH_API_KEY` (aliases
  `LANGCHAIN_API_KEY`, endpoints `LANGSMITH_ENDPOINT`/`LANGCHAIN_ENDPOINT`,
  project `LANGSMITH_PROJECT`/`LANGCHAIN_PROJECT`), `SENTRY_DSN`. No key
  values in code, docs, logs or fixtures.
- Kill switch: `enabled=False` constructor flag (detail: "exporter disabled
  (kill switch)"); the harness config flag `observability.export_enabled`
  gets wired to it at TM-C merge (B3 transport owns the enqueue path).

## Tests

`tests/test_sinks.py` — 24 tests:

- 22 unit tests, no network: payload shape, verbatim envelope mapping,
  redaction gate (extra key → reject), in-order partial accept, dedupe,
  server-duplicate 409 handling, missing-credential and kill-switch behavior,
  tenant/workspace resolution (env header, discovery once-per-batch, discovery
  failure fails closed, injected-post-without-get skips discovery), Sentry tag
  normalization, DSN parsing. Sentinel-marker scan (`SENTINEL-PROMPT-TEXT`,
  etc.) over the actual exported payloads: no hits.
- 2 integration tests (real sandbox): SKIP without credentials — which are
  otherwise loaded unattended by the root `conftest.py`
  `load_runtime_credentials` fixture from the approved runtime key file
  (overridable via `LANGSMITH_API_KEY_FILE`); they deliver to the live
  project, poll the SDK read path (bounded retry — LangSmith indexes runs
  asynchronously), PROVE arrival via `langsmith.Client(workspace_id=...)
  .list_runs(run_ids=[...])`, then run the §3.3 negative marker scan over the
  round-tripped runs.

Run (no credentials present → integration legs skip, unit legs run):

```bash
python -m pytest tests/test_sinks.py -v
```

## Live sandbox flow (exact commands; env var NAMES only, never values)

Credentials enter via the approved secret channel; this doc records the
mechanism, not any secret. LangSmith key file: `C:/svc/secrets/langsmith-api-key.txt`
(as provisioned by Trevor; the second file `langsmith-api-key-openrouter-dest.txt`
resolves to the SAME tenant/workspace and role — an equivalent credential, see
evidence below). The tests load it unattended at runtime:

```bash
python -m pytest tests/test_sinks.py -v          # unit (22) + integration (2)
python -m pytest tests/test_sinks.py -k integration -v   # live legs only
```

Expected on success: both integration tests pass unattended — the end-to-end
test delivers 3 events (POST /runs → 202) and reads back exactly the three
deterministic run ids via the SDK; the second test delivers one more event and
runs the marker scan over the round-tripped run. Re-runs are idempotent: the
re-POST of an already-received run id gets 409, which counts as
accepted-duplicate.

## Verification evidence

### Unit + redaction negative tests — PASS (2026-09-19)

```
ruff format --check . && ruff check .   -> all clean
python -m pytest tests/ -v              -> 652 passed, 1 failed (pre-existing)
python -m pytest tests/test_sinks.py -v -> 24 passed, 3 warnings in 5.07s
```

The single full-suite failure
(`tests/test_sync_profiles.py::test_plugin_status_detects_install_via_symlink`)
is a pre-existing Windows-environment privilege error (`OSError: [WinError
1314]` creating a symlink in tmp) — reproduced identically on the clean
checkpoint commit e3586c0 with this lane's changes stashed. Unrelated to sinks.

### LangSmith live leg — RESOLVED: tenant scoping, not a bad credential

Method: key loaded at runtime from the approved file into env (never printed,
never logged; only statuses, response metadata and response bodies below,
which contain no secret material). Probes ran against the pinned default
endpoint `https://api.smith.langchain.com`.

Root cause: the LangSmith API is tenant-scoped. The `lsv2_sk_` key
AUTHENTICATES fine via `x-api-key` — `GET /api/v1/workspaces` returns 200 —
but every tenant-scoped endpoint (write path `POST /runs`, read path
`GET /api/v1/sessions`) returns 403 unless the request also carries
`X-Tenant-Id: <workspace id>`. Without it the server cannot resolve the
workspace for the key (the workspace's `tenant_handle` is null, so
name-based resolution has nothing to match either). The prior checkpoint's
conclusion ("credential present but unauthorized") was wrong on mechanism:
the credential was authorized, the request was incomplete. The OpenAPI spec
at `/openapi.json` declares `X-Tenant-Id` as its "Tenant ID" security scheme.

| Attempt | Auth form | Endpoint | Result |
|---|---|---|---|
| 1 | `x-api-key` | `GET /api/v1/sessions` | 403 Forbidden (problem+json) |
| 2 | `X-Service-Key` / `Authorization: Bearer` | `GET /api/v1/sessions` | 401 `{"detail":"Invalid token"}` (same as no-auth — header not honored for this scheme here) |
| 3 | `x-api-key` | `POST /runs` | 403 `{"error":"Forbidden"}` |
| 4 | `X-Workspace-Id` / `X-Organization-Id` (each, and combined with `x-api-key`) | `POST /runs`, `GET /api/v1/sessions` | 403 — these headers do not scope the request |
| 5 | `x-api-key` | `GET /api/v1/workspaces` | **200 OK** — key authenticates; first non-deleted workspace: id `f729b10e-eda5-471f-8c03-7ff510343904`, org `01a0b584-62af-7b32-8bc5-8873e1cfa845`, display name "Workspace 1", role Admin (full `projects:*`, `runs:create`, `runs:read` permission set), `tenant_handle: null`, `data_plane_url: null` |
| 6 | `x-api-key` + `X-Tenant-Id: <workspace id>` | `GET /api/v1/sessions` | **200 OK** — 3 projects visible |
| 7 | `x-api-key` + `X-Tenant-Id: <workspace id>` | `POST /runs` | **202 `{"message":"Run created"}`** — real ingestion |
| 8 | `x-api-key` + `X-Tenant-Id: <workspace id>` | `GET /api/v1/runs/{id}` | **200 OK** — run read back with metadata |
| 9 | second key file `langsmith-api-key-openrouter-dest.txt` (same family, same length) | `GET /api/v1/workspaces` + `GET /api/v1/sessions` with its tenant header | same workspace `f729b10e-…`, same Admin role, same 3 projects — an equivalent credential for the same tenant |

Workspace (tenant) and project metadata (identifiers returned to any
authenticated caller, not secrets):

- tenant/workspace id: `f729b10e-eda5-471f-8c03-7ff510343904`
- sandbox project: `sovav-hermes-sandbox`, id `a5de9578-6b88-43ca-b71a-99e27b2b3d0e`
  (siblings visible: `sovav-hermes-openrouter`, `sovav-hermes-operations`)

Two live-behavior findings encoded in the exporter/tests:

1. **Duplicate create → 409**: re-POSTing an already-received run id returns
   409 `{"error":"Run create payload already received. Duplicate run create
   requests for the same run are not supported."}` — there is no upsert.
   The deterministic per-event run id + 409-as-accepted-duplicate makes
   transport retries idempotent (unit-tested via a 409-raising transport).
2. **Async indexing**: a 202-accepted run is not immediately visible to
   `list_runs` (observed lag: a few seconds; a probe run delivered at 16:00
   was read back within the same minute). The integration tests poll the
   read path with a bounded retry (60 s, 3 s interval) and fail on the real
   state if the deadline passes.

### LangSmith real ingestion + SDK read-back — PASS (live, 2026-09-19)

Schema pinned upstream (`C:/tmp/sovav-hermes/r-obs-00/docs/telemetry/
correlation-envelope.schema.json`) re-verified this lane:
sha256 `5271d5faa8bee8ad3d58885f4ab54f88d77af6e0620c78ba627665ca8433e70a` —
matches the value above.

Unattended test run (credentials loaded from the approved key file by the
conftest fixture, no human input):

```
python -m pytest tests/test_sinks.py -v
...
tests/test_sinks.py::test_integration_langsmith_sandbox_end_to_end PASSED
tests/test_sinks.py::test_integration_langsmith_readback_is_metadata_only PASSED
======================= 24 passed, 3 warnings in 5.07s =========================
```

What the passing assertions prove, on real round-tripped data:

- Write path: 4 synthetic metadata-only envelope events (`ev-0901`–`ev-0904`)
  delivered by `LangSmithExporter.deliver` to `POST /runs` (202) into
  `sovav-hermes-sandbox`.
- Read path: official `langsmith` SDK 0.13.0,
  `Client(workspace_id=exp.workspace_id).list_runs(project_name=..., run_ids=[...])`
  returned all 4 deterministic run ids (uuid5 of the event ids — e.g.
  `84c11775-d8fc-5fcc-8fe2-d4612362a773` for `ev-0901`), each with:
  `inputs = {}`, `outputs = None` (metadata-only round-trip), and
  `extra.metadata` carrying the envelope VERBATIM: `task_id t_763d8d58`,
  `profile/role worker`, `worktree/branch wt/htb-b4`, `board sovav-hermes`,
  `provider openrouter`, plus `event_id`/`enqueued_at`.
- §3.3 negative scan over the serialized real runs: no sentinel marker
  (`SENTINEL-PROMPT-TEXT`, `SENTINEL-RESPONSE-TEXT`, `SENTINEL-TOOL-RESULT-BODY`,
  `SENTINEL-SECRET-VALUE`, `SENTINEL-ENV-VALUE`, `SENTINEL-FILE-CONTENT`)
  appears anywhere in the round-tripped payloads.

## Not done / pending

- LangSmith leg: COMPLETE (auth resolved, real ingestion, SDK read-back,
  unattended integration tests, this documentation). Nothing pending.
- Sentry/Seer legs (credential gaps, unchanged): still needs `SENTRY_DSN` for
  the harness sandbox project (§2.2); `SENTRY_AUTH_TOKEN` if scripted API
  read-back is to be verified. Seer (§6): still needs Seer enabled + API
  access. Both remain explicit gaps; they do not gate the LangSmith leg.
- TM-C integration: wiring these sinks behind B3's durable transport + the
  `observability.export_enabled` kill switch (explicitly deferred to merge).
