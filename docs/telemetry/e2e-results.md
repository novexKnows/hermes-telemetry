# HTC-C3 — Controlled e2e integration test results (card t_9ebff8b3)

Status (2026-09-19, branch `wt/htc-c3-e2e` on integration tip `9a66874`):
**ALL LEGS VERIFIED — sandbox legs LIVE (no mock fallback needed, nothing
fabricated).** Every claim below is backed by a command and its real output;
evidence is redacted (ids, counts, timestamps — no payloads, no secret
values, ever).

## Scope and isolation protocol

- Test code: `tests/test_e2e.py` (10 tests). Standalone evidence driver:
  `tools/e2e_evidence.py` (prints one redacted JSON evidence record).
- EVERY run executes under a disposable home: BOTH `HERMES_HOME` and
  `HERMES_TELEMETRY_HOME` are pinned to a per-run temp directory (paths.py
  precedence — HERMES_TELEMETRY_HOME outranks HERMES_HOME, so both must be
  pinned; see the 2026-09-19 leak incident in conftest.py). `HOME` and
  `USERPROFILE` are pinned as a second layer.
- **THREE-PIN ISOLATION (operator ruling, 2026-09-19, harness-wide):**
  `HERMES_KANBAN_DB` and `HERMES_KANBAN_BOARD` are ALSO pinned to a
  disposable board DB / disposable board slug (`e2e-disposable-board`) in
  every run — a worker context CAN mutate the live board through inherited
  env (the runtime fence only protects delegate_task child contexts), and
  the C1 gate-2 probe demonstrated the leak. The disposable board slug is
  self-identifying on purpose: any leaked row carries it.
- **Leak checks (session-scoped, read-only):** (a) the REAL telemetry home
  is inventoried at session start and re-checked at session end — this run
  must create NO files there (transient sqlite `-wal`/`-shm` sidecars from
  an unrelated live process are reported, never asserted); (b) the LIVE
  board DB's `tasks` row count is captured before and after the session and
  must be unchanged (any delta fails the session).
- Credentials enter AT RUNTIME from the approved secret channel
  (`C:/svc/secrets/langsmith-api-key.txt`,
  `C:/svc/secrets/sentry-personal-token.txt`); env override names
  `LANGSMITH_API_KEY_FILE` / `SENTRY_AUTH_TOKEN_FILE`. Values are never
  printed, logged, hashed, attached or copied. The Sentry DSN is fetched at
  runtime from the authenticated keys endpoint (B4b-verified flow).
- A session-scoped proof fixture inventories the REAL telemetry home at
  session start and asserts at session end that this run created NO files
  there (transient sqlite `-wal`/`-shm` sidecars from an unrelated live
  process are reported, never asserted).

## Verdict summary

| Leg | What is proven | Verdict |
|-----|----------------|---------|
| A — LangSmith | emit → durable Transport → flush → live sandbox project → SDK read-back of all 3 deterministic run ids | **verified (live)** |
| A — Sentry | emit → durable Transport → flush → DSN store endpoint → authenticated API read-back of both per-run event ids, fresh-window proof | **verified (live)** |
| A — Seer | B4b analysis loop re-verified live on this tree: setup-check → fresh event ingest → per-event read-back → forced-event summarize (test passed 1/1 in 14s) | **verified (live)** |
| B — Metrics | controller metrics present in emitted envelope events (integrated handler vocabulary + B2 extraction layer), joined by envelope `task_id`/`run_id` | **verified** |
| C1 — Outage | sink outage: 4/4 events stay pending (journaled == pending), receipts queryable, outage ledger opens then closes, `/telemetry-status` reports it, recovery drains FIFO with zero loss | **verified** |
| C2 — Fail-closed | violation events (U+200B-obfuscated probes) → ZERO POSTs at BOTH layers (transport pre-journal gate; exporter pre-send gate), and fail-closed ≠ fail-stopped (clean events flow around a violation) | **verified** |
| C3 — Rollback | disposable home pre-test inventory (empty) restored by teardown (sha256 inventory proof); real telemetry home gains no files during the session | **verified** |

Credential gate: not triggered — all live legs ran against the real sandbox
endpoints. No leg required the mock fallback; nothing is mock-only.

## Leg A — sandbox sinks (live, redacted evidence)

Commands (the pytest suite loads credentials unattended via the conftest
runtime loader + the e2e module's runtime file loading; values never inline):

```
python -m pytest tests/test_e2e.py -v
python tools/e2e_evidence.py
```

Run: `10 passed, 5 warnings in ~20s` (both live legs pass unattended).

### LangSmith (evidence from tools/e2e_evidence.py, 2026-09-19T18:34Z)

- Write path: `envelope.emit()` → `Transport.enqueue` (durable journal,
  receipts queryable pre-flush) → `flush()` → `LangSmithExporter.deliver`:
  `{"ok": true, "accepted": 3, "detail": "langsmith[sovav-hermes-sandbox]:
  accepted 3, deduped 0"}`; status after flush: `pending 0`,
  `outage_open false`.
- Read path: official `langsmith` SDK, `Client(workspace_id=...)`
  `.list_runs(project_name="sovav-hermes-sandbox", run_ids=[...])` returned
  all 3 deterministic run ids (uuid5 of the transport event ids):
  `1537c814-1163-5002-92ab-c010e81f5fb8`,
  `5ffea327-905f-5741-a32c-4648bd6fa540`,
  `f54f755e-e9f7-5d70-aebe-f3335fc4b1b3`.
- Round-trip assertions on real read-back runs: `inputs`/`outputs` empty
  (metadata-only), envelope join keys verbatim in `extra.metadata`
  (`task_id=t_9ebff8b3`, `run_id=htc-c3-e2e`,
  `profile/role=worker`, `worktree/branch=wt/htc-c3-e2e`,
  `board=e2e-disposable-board` (THREE-PIN disposable slug),
  `trace_id=trc-…`), tenant workspace `f729b10e-eda5-471f-8c03-7ff510343904`.
- §3.3 negative marker scan over the serialized real runs: **no hits**.

### Sentry (evidence from tools/e2e_evidence.py, 2026-09-19T18:34Z)

- Write path: 2 metadata-only envelopes through the durable transport →
  `SentryExporter.deliver` to the DSN-derived store endpoint of sandbox
  project `sovav-hermes-sandbox` (project id `4512113840685056`):
  `{"ok": true, "accepted": 2, "detail": "sentry[4512113840685056]:
  accepted 2, deduped 0"}`.
- Read path: authenticated per-event GET
  `/api/0/projects/sovav/sovav-hermes-sandbox/events/{event_id}/` (polled;
  async ingest) returned both events:
  `ea33aaebc4b15ea5860036e29e2f8a9c` (dateCreated 2026-09-19T18:34:44.429Z),
  `88243f61b69054ff9d0ee6d5e2be06ff` (dateCreated 2026-09-19T18:34:44.764Z).
  Both `dateCreated` values fall inside THIS run's delivery window
  (fresh-window proof — an old object cannot pass).
- Tag round-trip: `task_id=t_9ebff8b3`, `run_id=htc-c3-e2e`,
  `profile_role=worker`, `worktree_branch=wt/htc-c3-e2e` on both events;
  `context.envelope` carries the envelope verbatim.
- §3.3 negative scan over the real read-back bodies: **no hits**.

## Leg B — metrics (mapping table:
docs/telemetry/utilization-metrics-mapping.md)

Two layers are asserted (`test_e2e_legB_metrics_in_integrated_emission`,
`test_e2e_legB_b2_extraction_mapping_layer`):

1. INTEGRATED EMISSION — the real plugin hook handlers are registered and
   fired exactly as the runtime fires them (`__init__.register` + hook
   dispatch). The emitted event body carries the controller-consumable
   metrics in the redaction-allowlist vocabulary: `tokens_in`, `tokens_out`,
   `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `cost_usd`
   (priced through the disposable-home pricing engine), `latency_ms`,
   `api_call_count`, `finish_reason`, `estimated`, `moa_reference_count`,
   plus tool (`tool_name`, `ok`, `status`), delegation (`child_status`,
   `duration_ms`), cron (`cron_job_id` anchored parse) and run-outcome
   (`status` ok/interrupted/error) metrics. Content bodies (MoA advisor
   output, tool args/result, child summary) are proven absent from the
   emitted records.
2. B2 EXTRACTION — `metrics.py` functions asserted against the mapping
   table: token buckets incl. derived `prompt_tokens`/`total_tokens`,
   `api_duration_ms`, TTFB when streamed, billing rows (dirty installed
   tree: `billed_cost_usd`, `provider_response_id`, `serving_provider`),
   MoA allowlisted projection (`output`/`input_messages` discarded),
   `hook_task_id` trap (per-turn UUID never becomes canonical `task_id`),
   dispatch observers, `GAPS == 13` (full metric-name set pinned).
- Envelope structure per table A: all 12 base fields present; Turnstone
  extensions null on harness events; metrics ride OFF the envelope
  (schema `additionalProperties: false`); the join keys survive the durable
  transport path (LocalFileSink round-trip asserted).

## Legs C — failure / rollback

### C1 — transport outage, no silent loss

Sink unavailable (TimeoutError-raising sink, mirroring a network outage):

- `flush()` fails closed: `pending == 4`, `journaled == 4`; receipts remain
  queryable through the outage (`delivered: false`).
- `/telemetry-status json`: `{"pending": 4, "outage_open": true, ...}`;
  the human surface prints the "nothing was lost" advisory.
- Outage ledger (`outage_ledger.jsonl`): an `open` episode with
  `pending_count == 4`, the pending event ids and staleness, detail carrying
  `TimeoutError`; a `close` entry after recovery.
- Recovery: a healthy sink over the SAME store drains 4/4 in journal (FIFO)
  order — trace-id sequence match, zero loss, zero reorder; status resets
  (`pending 0`, `outage_open false`, `last_success_at` set).

### C2 — redaction fail-closed, zero POSTs

Probes (all Unicode default-ignorable-obfuscated with U+200B, which cannot
defeat either scan):

- Transport pre-journal gate: content sentinels (`PROMPT_TEXT`,
  `RESPONSE_TEXT`, `FILE_CONTENT`, direct and obfuscated) and secret-shaped
  strings (`sk-…`, `AKIA…` patterns) → `ValueError` BEFORE journal append;
  journal stays empty; sink sees ZERO calls.
- Exporter pre-send gate: a record carrying `SENTINEL-SECRET-VALUE`
  (obfuscated) in the schema-allowed `model` field is rejected by BOTH
  `LangSmithExporter` and `SentryExporter` with `ok=false`, `accepted=0`
  and **zero POSTs**; the rejection detail names no value.
- Fail-closed ≠ fail-stopped: a violation rejects exactly its own event;
  clean events enqueued before and after it flow normally (2/2 delivered).

### C3 — rollback, inventory + hash proof

- Per-test: sha256 inventory of the disposable home pre-run (empty) → after
  the pipeline (artifacts present, every path inside the root) → after
  teardown (root removed; inventory identical to pre-test).
- Session-wide: the real telemetry home's file inventory is snapshotted at
  session start and re-checked at session end — this run created no files
  there. `paths.get_telemetry_home()` is additionally asserted to never
  resolve to the real home.

## Full-suite verification (CI parity)

```
ruff format --check .   -> all clean (68 files)
ruff check .            -> All checks passed!
python -m pytest tests/ -q
  -> 1 failed, 866 passed, 3 skipped in 116.26s
```

The single failure is
`tests/test_sync_profiles.py::test_plugin_status_detects_install_via_symlink`
— the pre-existing Windows privilege error (`OSError: [WinError 1314]`
creating a symlink in tmp), reproduced identically on the base by the
HTC-C0-REVIEW lane (t_c41b91f5); zero new failures. 870 collected = 860
prior + 10 new e2e tests.

## Carry-forward acceptance (TM-B handoffs)

- **THREE-PIN ISOLATION (operator ruling, mid-run):** implemented in both
  `tests/test_e2e.py` and `tools/e2e_evidence.py` — `HERMES_KANBAN_DB` +
  `HERMES_KANBAN_BOARD` pinned alongside the two home vars, disposable board
  slug self-identifying (`e2e-disposable-board`), live-board `tasks` row
  count checked read-only before/after each session. Note for the record:
  THIS lane's own code never touches kanban surfaces (no kanban imports in
  the e2e path); the leak the operator cited (t_db9e21e0) is treated as a
  harness-wide hazard and the pins are preventive. Live board untouched by
  this lane; t_db9e21e0 left entirely to the operator.

- **Quarantined rows in the REAL telemetry DB**
  (`C:/Users/Sovav/AppData/Local/hermes/telemetry/telemetry.db`,
  `llm_calls.id=5947`, `session_id='proof_sess_001'`): checked READ-ONLY —
  the row is still present, untouched by this lane. Searched the plugin
  surface for a supported cleanup API (`db.py`, `telemetry_cli.py`,
  `__init__.py`): **no purge/delete/quarantine API exists**. Disposition:
  the rows stay quarantined in place; nothing in this lane touched the real
  DB. A supported cleanup API (or a documented manual purge) is upstream
  work.
- **G1 auxiliary-call_llm gap**: plugin-side only; the runtime
  (`agent/auxiliary_client.py::call_llm`) fires no hooks. Recorded as a
  cross-repo dependency for the PM (below); the plugin does NOT hack the
  runtime.
- G2–G7, HARNESS-A: referenced, not re-litigated (cross-repo / routed).

## Cross-repo dependencies for the PM

1. `agent/auxiliary_client.py::call_llm` needs hook dispatch (G1) so
   auxiliary-call tokens/cost become observable (runtime repo).
2. Quarantined-row cleanup: either a supported plugin purge API or an
   operator-documented manual SQL cleanup for the real telemetry DB
   (`llm_calls.id=5947`, `session_id='proof_sess_001'`).
3. (pre-existing, routed by TM-B) G2–G7 / HARNESS-A as recorded on t_8dd9e625.

## Reproduce

```
cd C:/tmp/hermes-telemetry/c3-e2e        # branch wt/htc-c3-e2e
python -m pytest tests/test_e2e.py -v    # full e2e suite (live legs included
                                         # when the approved key files exist)
python tools/e2e_evidence.py             # redacted JSON evidence record
```

Live legs SKIP (never fail, never fabricate) when credentials are absent or
rejected — the credential gate.
