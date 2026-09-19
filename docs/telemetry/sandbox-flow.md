# Sandbox telemetry flow — LangSmith (+ Sentry) exporters (HTB-B4 / B4b)

Status (2026-09-19, merged lanes B4a + B4b): **Sentry + Seer legs VERIFIED
live end-to-end** (lane B4b); the LangSmith leg is recorded by lane B4a as
**VERIFIED end-to-end (live)** — the auth blocker was a tenant-scoping gap,
not a bad credential (resolution and evidence below) — while lane B4b's
correction rerun still saw the provisioned credential rejected by the live
API (its evidence is preserved below; both lane records are kept as
written, with attribution). Nothing in this flow is fabricated: every
claim of "verified" below is backed by a command and its real output.

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

## Sentry + Seer live flow (HTB-B4b) — VERIFIED 2026-09-19

### Provisioning (authenticated API metadata only — nothing guessed)

Org resolved via `GET /api/0/organizations/` with the personal token: exactly
one org, `sovav` (id `4512063704989696`). Zero pre-existing projects. Sandbox
project created per Trevor's authorization:
```
POST /api/0/organizations/sovav/projects/  {"name": "sovav-hermes-sandbox", "platform": "python"}
-> HTTP 201, project id 4512113840685056, slug sovav-hermes-sandbox
```

DSN obtained from the project's single client key:

```
GET /api/0/projects/sovav/sovav-hermes-sandbox/keys/  -> 1 key "Default"
DSN ingest host: o4512063704989696.ingest.us.sentry.io   # host name only, not a secret
```

Token topology (names only): `C:/svc/secrets/sentry-personal-token.txt` is a
user auth token (`sntryu_`-family) — full API read access, used for
provisioning/read-back. `C:/svc/secrets/sentry-org-token.txt` (`sntrys_`-family)
authenticates (HTTP 200 on `GET /api/0/`) but is 403 on org/project listing
(scoped narrower than org read) — unused; least privilege.

### Ingestion + read-back (the verified loop)

Env vars (names): `SENTRY_DSN` (store credential), `SENTRY_AUTH_TOKEN` (API
read-back), `SENTRY_ORG` (`sovav`), `SENTRY_PROJECT` (`sovav-hermes-sandbox`).

```
export SENTRY_DSN=...            # from the keys endpoint above, never inline in code
export SENTRY_AUTH_TOKEN=...     # personal token file contents
python -m pytest tests/test_sinks.py -k "integration and sentry" -v
```

Verified live, in this order:

1. `SentryExporter.deliver()` posted 3 metadata-only synthetic envelopes to the
   DSN-derived store endpoint
   `https://o4512063704989696.ingest.us.sentry.io/api/4512113840685056/store/`
   (X-Sentry-Auth store protocol from the DSN's public key) — result
   `{"ok": true, "accepted": 3, "detail": "sentry[4512113840685056]: accepted 3, deduped 0"}`.
2. Read-back via `GET /api/0/projects/sovav/sovav-hermes-sandbox/events/{event_id}/`
   with `Authorization: Bearer $SENTRY_AUTH_TOKEN`. Caveat observed, not
   assumed: the per-event GET returned 404 immediately after ingest
   (asynchronous ingest processing) and HTTP 200 for all 3 events after ~60s —
   the integration test therefore polls (deadline 120s, 10s interval).
3. Read-back payloads carry the envelope round-tripped: tags
   `profile_role=worker`, `worktree_branch=wt/htb-b4b`, `task_id=t_e3985496`,
   `trace_id=trc_htb4b_0002`, `run_id=22`, `event_id=ev-000N`, plus
   `context.envelope` verbatim (slash keys preserved) and `context.enqueued_at`.
   The §3.3 sentinel-marker scan over the real read-back JSON: no hits.

Sentry event ids read back (deterministic uuid5 of the transport event ids):
`67c3e37029315df9b5434da35e08bdc9`, `85242506802258088112298b07ab7930`,
`bf1cbb73a50e5c04bafdf2d63e85a8c9`.

> R4b correction note: the event ids above belong to the ORIGINAL 16:04Z run.
> From the R4b correction onward (see next section), integration tests
> generate UNIQUE event/trace/handoff ids per run, so a read-back can only
> ever succeed against objects ingested by that run — fixed ids proved old
> objects, not fresh ingestion.

### Correction re-verification (R4b, 2026-09-19, commit range e3586c0..htb-b4b)

Two fixes re-verified LIVE against the same sandbox project/DSN (org `sovav`,
project id `4512113840685056`; no other project exists on the token):

1. Never-export gate (fail-closed, pre-send, both exporters): `sinks.py` now
   strips Unicode default-ignorable characters (Cf/Zl/Zp + outliers — the
   same set as B1's `redaction.py`, shared helper `_strip_default_ignorables`)
   from the ENTIRE outbound payload before never-export sentinel matching.
   A hit (direct or obfuscated, e.g. `SENTINEL-SECRET<U+200B>-VALUE` in the
   schema-allowed `model` field) rejects the event with ZERO transport calls;
   `detail` names the field path only — never the rejected value. Clean
   values travel in the normalized (stripped) form. Unit probes cover direct,
   case-normalized, U+200B, U+2063, U+E0020 tag-char and U+180E variants for
   BOTH exporters plus benign-ignorable positives (14 tests).
2. Fresh-ID integration proof: all three integration legs (Sentry e2e,
   Sentry metadata-only scan, Seer loop) now use per-run unique
   event/trace/handoff ids, poll/read back THAT exact event, and join Seer by
   the fresh event's own `groupID` (from the authenticated per-event
   read-back) — never by the shared title. The Seer summarize call pins the
   event via the endpoint's `force_event_id` (POST body `{"event_id": ...}`,
   verified against live getsentry `src/sentry/seer/endpoints/
   group_ai_summary.py` + `src/sentry/seer/autofix/issue_summary.py`: forced
   events always generate a new summary and the response's `eventId` names
   it); the test asserts the returned analysis carries the fresh id.

Fresh evidence from the correction run (ids unique per run; created
2026-09-19T16:44:21Z, i.e. AFTER the R4b review rerun at 16:15Z):

- deliver: `{"ok": true, "accepted": 1, "detail": "sentry[4512113840685056]: accepted 1, deduped 0"}`
- read-back: HTTP 200, `eventID d9aa3415097155f8a9be0036cf068be6`,
  `groupID 7742538801`, tags round-trip `event_id=ev-41afed869cad`,
  `trace_id=trc-78bf59bd`, `handoff_id=hnd-413593cc21db` (non-null),
  `task_id=t_e246539f`, `run_id=28`.
- summarize (forced to the fresh event): HTTP 200, response `eventId`
  `d9aa3415097155f8a9be0036cf068be6` — the fresh id, not an older sibling.
- autofix: HTTP 409 `repos_not_linked` (unchanged gap — needs repo
  integration + code mapping, org-level, not attempted per least privilege).

Commands (values never printed; DSN fetched from the authenticated keys
endpoint at runtime):

```
export SENTRY_AUTH_TOKEN="$(tr -d ' \r\n' < /c/svc/secrets/sentry-personal-token.txt)"
export SENTRY_ORG=sovav SENTRY_PROJECT=sovav-hermes-sandbox
export SENTRY_DSN="$(curl -s -H "Authorization: Bearer $SENTRY_AUTH_TOKEN" \
  https://sentry.io/api/0/projects/$SENTRY_ORG/$SENTRY_PROJECT/keys/ \
  | python -c "import json,sys;print(json.load(sys.stdin)[0]['dsn']['public'])")"
python -m pytest tests/test_sinks.py -k "integration and sentry" -v
python -m pytest tests/test_seer.py -k integration -v
```

### Seer (verified summary loop; autofix run gap recorded)

Endpoints pinned against live `getsentry/sentry` `src/sentry/api/urls.py`
(master, fetched 2026-09-19) — not memory:

- `GET  /organizations/sovav/seer/setup-check/` -> HTTP 200
  `{"setupAcknowledgement": {"orgHasAcknowledged": true, "userHasAcknowledged": true}, "hasFreeAutofixAccess": false, "billing": {"hasAutofixQuota": true, "hasScannerQuota": true}}`
- `GET  /projects/sovav/sovav-hermes-sandbox/seer/preferences/` -> HTTP 200
  (repos `[]`, `automated_run_stopping_point: "code_changes"`)
- Our 3 ingested events grouped into issue `7742538801`
  (`SOVAV-HERMES-SANDBOX-1`, title = `R-OBS-00 metadata-only telemetry event`).
- **Analysis loop VERIFIED**: `POST /organizations/sovav/issues/7742538801/summarize/`
  -> HTTP 200 with a real Seer analysis (`headline`, `whatsWrong`,
  `possibleCause`, `scores.possibleCauseConfidence`) referencing our event id.
  Implemented as the usable client `seer.py::SeerClient` (stdlib-only;
  `setup_check` / `project_preferences` / `summarize_issue` /
  `trigger_autofix` / `autofix_status`; token via `SENTRY_AUTH_TOKEN` env name
  only). R4b correction: `summarize_issue` accepts `event_id`, mapped to the
  endpoint's `force_event_id` (verified against live getsentry source) —
  without it Seer picks the event itself and returned an OLDER sibling of the
  group; with it the response's `eventId` is exactly the event being proven.
- **Autofix run gap (explicit)**: `POST .../autofix/` -> HTTP 409
  `{"code": "repos_not_linked"}` — Seer autofix runs require repositories
  connected to the project plus code mappings. No repo integration is
  provisioned on the org; connecting one was not attempted (org-level change,
  beyond sandbox-project least privilege). The status/read loop itself
  (`GET .../autofix/` -> `{"autofix": null}`) is verified. Unblocking item:
  a repo integration + code mapping for the sandbox project, or a decision to
  run Seer analysis in summary-only mode.

### Tests (skip-gated, mirroring the LangSmith pattern)

- `tests/test_sinks.py`: 37 unit + 2 Sentry integration
  (`test_integration_sentry_sandbox_end_to_end` — deliver + polled API
  read-back of a FRESH per-run event id + tag round-trip + `dateCreated`
  fresh-window assertion (creation timestamp >= a pre-delivery UTC bound,
  small 5s skew allowance); `test_integration_sentry_readback_is_metadata_only`
  — §3.3 negative scan over the real read-back payload of a fresh event,
  same `dateCreated` bound). Unit tests include 14 never-export-gate probes:
  direct, case-normalized and Unicode-obfuscated sentinel hits in the
  schema-allowed `model` field rejected with ZERO transport calls for BOTH
  exporters, plus benign-ignorable normalization positives, PLUS 4
  key-negative probes (sentinel material used as a mapping KEY — U+2063
  obfuscated and direct — rejected with ZERO transport calls, the rejection
  detail carrying an opaque structural location only: raw and normalized key
  text never echoed, either exporter).
- `tests/test_seer.py`: 9 unit + 1 integration
  (`test_integration_seer_sandbox_analysis_loop` — setup-check, ingest a
  FRESH metadata-only event with per-run unique event/trace/handoff ids,
  read back THAT exact event (with the same `dateCreated` fresh-window
  assertion), join its issue group via the event's own
  `groupID`, forced-event `summarize_issue` asserting the fresh id in the
  returned analysis, §3.3 scan, `trigger_autofix` outcome surfaced without
  asserting success).
- Integration legs skip unless `SENTRY_DSN` + `SENTRY_AUTH_TOKEN` are present
  in the ambient env (no-fabrication gate). Live run: `3 passed, 2 skipped`
  (the 2 LangSmith legs skip — lane B4a credential issue, see below).

## Verification evidence

### Unit + redaction negative tests — PASS (2026-09-19)

```
ruff format --check . && ruff check .   -> all clean
python -m pytest tests/ -v              -> 652 passed, 1 failed (pre-existing)
python -m pytest tests/test_sinks.py -v -> 24 passed, 3 warnings in 5.07s
```

Lane B4a's rerun record (kept as written):

The single full-suite failure
(`tests/test_sync_profiles.py::test_plugin_status_detects_install_via_symlink`)
is a pre-existing Windows-environment privilege error (`OSError: [WinError
1314]` creating a symlink in tmp) — reproduced identically on the clean
checkpoint commit e3586c0 with this lane's changes stashed. Unrelated to sinks.

### LangSmith live leg — lane B4a: RESOLVED (tenant scoping, not a bad credential)

Lane B4b's correction rerun record (kept as written):

```
python -m pytest tests/test_sinks.py tests/test_seer.py -q
39 passed, 5 skipped   # pre-live; integration legs skip without creds
```

### LangSmith live leg — lane B4b's run: BLOCKED (provisioned key rejected by the live API)

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

- LangSmith leg (lane B4a): COMPLETE per B4a's record (auth resolved, real
  ingestion, SDK read-back, unattended integration tests, this
  documentation). Lane B4b's correction rerun still saw the provisioned
  credential rejected by the live API — both records preserved above; if
  the live leg needs re-proof on the integrated tree, that is new work.
- Sentry/Seer legs (lane B4b): VERIFIED live per B4b's record. Remaining
  credential gaps as recorded by lane B4a: still needs `SENTRY_DSN` for
  the harness sandbox project (§2.2); `SENTRY_AUTH_TOKEN` if scripted API
  read-back is to be verified. Seer (§6): still needs Seer enabled + API
  access. Both remain explicit gaps; they do not gate the LangSmith leg.
- Seer autofix RUN, not the loop (lane B4b): needs a repo integration +
  code mapping on the sandbox project (HTTP 409 `repos_not_linked`), or an
  explicit decision to run Seer in summary-only mode.
- TM-C integration: wiring these sinks behind B3's durable transport + the
  `observability.export_enabled` kill switch (explicitly deferred to merge).
