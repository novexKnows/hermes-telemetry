# Sandbox telemetry flow — LangSmith (+ Sentry) exporters (HTB-B4)

Status: **WIP / credential gate hit** — exporters and tests are complete; the
live LangSmith sandbox leg is BLOCKED because the provisioned credential is
rejected by the live API (evidence below). Nothing in this flow is fabricated:
every claim of "verified" below is backed by a command and its real output.

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

- Live LangSmith ingestion + SDK read-back proof (blocked, evidence above).
- Sentry/Seer legs (credential gaps, tracked in the block reason).
- TM-C integration: wiring these sinks behind B3's durable transport + the
  `observability.export_enabled` kill switch (explicitly deferred to merge).
