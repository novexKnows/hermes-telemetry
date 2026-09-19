# Sandbox telemetry flow — LangSmith (+ Sentry) exporters (HTB-B4 / B4b)

Status (2026-09-19, lane B4b): **Sentry + Seer legs VERIFIED live end-to-end**;
the LangSmith live leg remains BLOCKED by its provisioned credential (evidence
below — lane B4a owns that leg). Nothing in this flow is fabricated: every
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
  (`langsmith_run_id_for_event`) so retries upsert instead of duplicating.
- Credentials (§3.4): read from env ONLY — `LANGSMITH_API_KEY` (aliases
  `LANGCHAIN_API_KEY`, endpoints `LANGSMITH_ENDPOINT`/`LANGCHAIN_ENDPOINT`,
  project `LANGSMITH_PROJECT`/`LANGCHAIN_PROJECT`), `SENTRY_DSN`. No key
  values in code, docs, logs or fixtures.
- Kill switch: `enabled=False` constructor flag (detail: "exporter disabled
  (kill switch)"); the harness config flag `observability.export_enabled`
  gets wired to it at TM-C merge (B3 transport owns the enqueue path).

## Tests

`tests/test_sinks.py` — 19 tests:

- 17 unit tests, no network: payload shape, verbatim envelope mapping,
  redaction gate (extra key → reject), in-order partial accept, dedupe,
  missing-credential and kill-switch behavior, Sentry tag normalization,
  DSN parsing. Sentinel-marker scan (`SENTINEL-PROMPT-TEXT`, etc.) over the
  actual exported payloads: no hits.
- 2 integration tests (real sandbox): SKIP without ambient credentials;
  otherwise deliver to the live project and PROVE arrival via the SDK read
  path (`langsmith.Client().list_runs(run_ids=[...])`), then run the §3.3
  negative marker scan over the round-tripped runs.

Run (no credentials present → integration legs skip, unit legs run):

```bash
python -m pytest tests/test_sinks.py -v
```

## Live sandbox flow (exact commands; env var NAMES only, never values)

Credentials enter via the approved secret channel; this doc records the
mechanism, not any secret. LangSmith key file: `C:/svc/secrets/langsmith-api-key.txt`
(as provisioned by Trevor). Load it into env at runtime:

```bash
export LANGSMITH_API_KEY="$(tr -d ' \r\n' < /c/svc/secrets/langsmith-api-key.txt)"
python -m pytest tests/test_sinks.py -k integration -v
```

Expected on success: both integration tests pass; the end-to-end test prints
nothing secret (assertion-only), and the read-back returns exactly the run ids
derived from the delivered event ids.

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
  only).
- **Autofix run gap (explicit)**: `POST .../autofix/` -> HTTP 409
  `{"code": "repos_not_linked"}` — Seer autofix runs require repositories
  connected to the project plus code mappings. No repo integration is
  provisioned on the org; connecting one was not attempted (org-level change,
  beyond sandbox-project least privilege). The status/read loop itself
  (`GET .../autofix/` -> `{"autofix": null}`) is verified. Unblocking item:
  a repo integration + code mapping for the sandbox project, or a decision to
  run Seer analysis in summary-only mode.

### Tests (skip-gated, mirroring the LangSmith pattern)

- `tests/test_sinks.py`: 17 unit + 2 Sentry integration
  (`test_integration_sentry_sandbox_end_to_end` — deliver + polled API
  read-back + tag round-trip; `test_integration_sentry_readback_is_metadata_only`
  — §3.3 negative scan over the real read-back payload).
- `tests/test_seer.py`: 8 unit + 1 integration
  (`test_integration_seer_sandbox_analysis_loop` — setup-check, ingest a fresh
  metadata-only event, locate its issue group, real `summarize_issue`, §3.3
  scan, `trigger_autofix` outcome surfaced without asserting success).
- Integration legs skip unless `SENTRY_DSN` + `SENTRY_AUTH_TOKEN` are present
  in the ambient env (no-fabrication gate). Live run: `3 passed, 2 skipped`
  (the 2 LangSmith legs skip — lane B4a credential issue, see below).

## Verification evidence

### Unit + redaction negative tests — PASS (2026-09-19)

```
17 passed, 2 skipped in 0.28s   # integration legs skip without creds
```

### LangSmith live leg — BLOCKED: provisioned key rejected by the live API

Method: key loaded at runtime from the approved file into env (never printed,
never logged; only statuses and response bodies below, which contain no secret
material). Write path `POST https://api.smith.langchain.com/runs` (the exact
endpoint the langsmith SDK 0.13.0 uses, verified from SDK source) and read
path `GET /api/v1/sessions` + SDK `list_projects`.

| Attempt | Auth form | Endpoint | Result |
|---|---|---|---|
| 1 | `x-api-key` header | `POST /runs` | 403 `{"error":"Forbidden"}` |
| 2 | `x-api-key` header | `GET /api/v1/sessions` | 403 Forbidden |
| 3 | `x-api-key`, official langsmith SDK 0.13.0 (`list_projects`, `create_run`) | both | 403 Forbidden |
| 4 | `X-Service-Key` header | `POST /runs` | 401 `{"error":"Unauthorized"}` |
| 5 | `x-api-key` + `X-Service-Key` | `POST /runs` | 403 Forbidden |
| 6 | control: deliberately-corrupted key, `x-api-key` | `GET /api/v1/sessions` | 403 — identical body to the real key, so the server is not distinguishing |
| 7 | regional host `api.eu.smith.langchain.com` | — | DNS does not resolve |

Key shape (diagnostic only): the provisioned token is an `lsv2_sk_`-family
LangSmith **service key** (51 chars, no whitespace) — service keys authenticate
via `X-Service-Key` per SDK source; that path returned 401 Unauthorized. The
`x-api-key` header (which the OpenAPI spec at `/openapi.json` declares as the
`API Key` security scheme) returns 403 Forbidden. No
`LANGSMITH_ENDPOINT`/`LANGSMITH_PROJECT` is provisioned anywhere, so the
default cloud endpoint and the sandbox project name `sovav-hermes-sandbox`
were used as dispatched.

Conclusion: the credential is PRESENT but UNAUTHORIZED for the pinned endpoint
— contract §3.4 failure route ("missing or unauthorized observability
credentials ⇒ needs_input to Trevor"). No ingestion, no read-back, no claims.

Needed to unblock (names only):

1. A LangSmith credential that authenticates on `https://api.smith.langchain.com`
   — e.g. a personal access token (`lsv2_pt_`-family) with access to the
   sandbox workspace/project — or confirmation of the correct endpoint/tenant
   for the provisioned `lsv2_sk_` service key.
2. The `sovav-hermes-sandbox` project exists or may be auto-created on first
   write (LangSmith creates sessions on first ingest; if a pre-created project
   is required, provision it).
3. Sentry leg (unchanged from run 1): still needs `SENTRY_DSN` for the harness
   sandbox project (§2.2); `SENTRY_AUTH_TOKEN` if scripted API read-back is to
   be verified. Seer (§6): still needs Seer enabled + API access. Both remain
   explicit gaps; they do not gate the LangSmith leg.

## Not done / pending

- Live LangSmith ingestion + SDK read-back proof (blocked, evidence above —
  lane B4a owns the fix).
- Seer autofix RUN (not the loop): needs a repo integration + code mapping on
  the sandbox project (HTTP 409 `repos_not_linked`), or an explicit decision
  to run Seer in summary-only mode.
- TM-C integration: wiring these sinks behind B3's durable transport + the
  `observability.export_enabled` kill switch (explicitly deferred to merge).
