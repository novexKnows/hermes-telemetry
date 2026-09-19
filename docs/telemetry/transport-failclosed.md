# Durable event transport — fail-closed outage/staleness matrix (HTB-B3)

Scope: `transport.py` (durable local queue for ratified R-OBS-00 envelopes),
the pinned sink interface consumed by B4 exporters, the outage ledger, and the
`/telemetry-status` surface. Envelope schema and redaction are owned by B1
(R-OBS-00 v1.0.0); the transport receives **already-redacted envelope dicts**
and ENFORCES this as a concrete metadata-only precondition before journal
append — envelopes that violate the pinned R-OBS-00 v1.0.0 schema shape
(sha256 `5271d5fa…e70a`) or carry secret-shaped / content-sentinel strings in
any field are REJECTED with `ValueError` before the journal is touched. The
transport never redacts and never silently drops.

## Design invariants (the fail-closed contract)

1. **Journal before sink.** `enqueue(envelope)` appends one JSONL line to
   `<telemetry home>/transport/events.jsonl` and fsyncs it BEFORE any sink
   attempt. The receipt `{event_id, journal_offset}` is durable the moment
   `enqueue` returns and remains queryable no matter what the sink does.
2. **Cursor-based resume.** `state.json` persists `cursor_offset` (byte offset
   through which every record is accepted at the sink), `delivered_count`,
   `last_success_at`, and `journal_size`. A crash resumes from the cursor:
   nothing before it is re-sent in normal operation, nothing after it is lost.
3. **Idempotent by event_id.** A crash between sink-accept and cursor-persist
   can cause redelivery (at-least-once). Redelivered records carry the same
   stable `event_id`; sinks MUST dedupe on it. `LocalFileSink` is the
   conformance implementation.
4. **Outage ledger.** Every delivery episode that ends with pending events
   opens a ledger entry (`outage_ledger.jsonl`) recording the undelivered
   event ids and the staleness (age of the oldest pending event); a later
   successful flush closes the episode. Every flush that still ends with
   pending records while an episode is ALREADY open durably refreshes it with
   an `update` entry (same `episode_id`, current pending set) — the ledger is
   never left stale by newly queued work. No outage is ever silent.
5. **No drop paths.** There is no code path that removes an event from the
   journal or leaves the pending count unaccounted. A torn final journal line
   (partial write) is truncated with a logged warning and a persisted
   `torn_truncations` counter — earlier complete records are never touched.

## Pinned sink interface (B4 conforms to this exact shape)

```python
class Sink:
    def deliver(self, events: list[dict]) -> dict: ...
    # returns {"ok": bool, "accepted": int, "detail": str}
```

Each event dict is:

```python
{
    "event_id": str,     # transport-assigned, stable across retries (dedupe key)
    "enqueued_at": str,  # ISO-8601 UTC of the original enqueue
    "attempt": int,      # 1-based delivery attempt counter
    "envelope": {...},   # R-OBS-00 envelope, verbatim, already redacted (B1)
}
```

Semantics: `accepted` counts events durable at the sink IN ORDER; everything
beyond the accepted prefix stays pending and is retried (partial accept). A
duplicate `event_id` counts as accepted and is not re-written. The result is
validated STRICTLY: all three fields must be present with exact types
(`ok: bool`, `accepted: int` — a real int, not bool — in `0..len(batch)`,
`detail: str`); any missing key, wrong type, or out-of-range count is a failed
attempt and NEVER advances the cursor. Timeout enforcement is the sink's
responsibility (B4 owns exporter HTTP timeouts); the transport treats any
exception or non-conforming result as a failed attempt and retries with
exponential backoff (`base * 2^n`, capped).

Delivery retry policy (defaults): `max_retries=5`, `base_delay_s=0.05`,
`max_delay_s=2.0`, `batch_size=100`. A success resets the retry budget for the
next batch. A partial accept ends the flush with the remainder requeued.

## Outage/staleness matrix

| # | Scenario | Expected behavior | Proof mechanism | Test |
|---|----------|-------------------|-----------------|------|
| 1 | Sink unavailable | flush fails closed: events stay pending (journaled == pending), receipts queryable, outage ledger opens with pending ids + staleness; restart with healthy sink drains everything in FIFO order, zero loss | status counts + `receipt()` + ledger entry + post-recovery drain order | `test_journal_survives_sink_down` |
| 2 | Sink slow / timeout | sink exceptions are failed attempts: bounded retries with exponential backoff (base·2ⁿ capped), outage ledger entry carries the error detail, events stay pending | recorded backoff delay sequence `[base, 2·base, 4·base]`, `attempts == max_retries`, ledger `detail` contains `TimeoutError` | `test_sink_timeout_retries_with_exponential_backoff` |
| 3 | Partial accept | only the accepted prefix advances the cursor; exactly the unaccepted remainder stays pending and is redelivered — no accepted event is resent to the sink | cursor/delivered counts after flush + second flush delivers exactly the tail batch | `test_partial_accept_requeues_only_unaccepted` |
| 4 | Crash mid-batch | cursor rewind (lost state) forces redelivery of the in-flight batch; the idempotent sink dedupes by event_id so the delivered store holds each event exactly once | rewind `cursor_offset`, restart, re-flush: delivered file has exactly N unique event ids | `test_crash_mid_batch_resumes_without_duplication` |
| 5 | Process restart | a new `Transport` over the same store reloads journal + state and drains all pending events without loss | pending count after restart == journaled before crash; delivered file complete after flush | `test_process_restart_drains_without_loss` |
| 6 | Stale / aged events | `oldest_pending_age_seconds` reports the age of the oldest pending event and resets to 0 with `last_success_at` set once drained | backdated enqueue timestamps via monkeypatched clock; status before/after drain | `test_staleness_surface_reports_oldest_pending_age` |
| 7 | Journal corruption on last line | torn final line (partial write) is truncated at the last complete record with a logged warning and `torn_truncations` incremented; earlier records intact and deliverable | caplog warning assertion + `torn_truncations == 1` + receipts resolve + full drain | `test_journal_corruption_torn_last_line` |

Supplementary non-matrix tests:

| Scenario | Test |
|----------|------|
| Receipts + status remain queryable while the sink never returns (flush blocked in another thread) | `test_receipts_queryable_while_sink_never_returns` |
| `LocalFileSink` conformance: `{ok, accepted, detail}` result; duplicate event_id accepted-but-not-rewritten | `test_local_file_sink_conformance_and_dedupe` |
| No sink configured → fail-closed (pending, not fatal), status reports `sink_configured: false` | `test_no_sink_configured_is_fail_closed_not_fatal` |
| Non-conforming sink result (missing `ok`) treated as failed attempt, not crash; ledger opens | `test_non_conforming_sink_result_treated_as_failure` |
| Malformed `{"ok": true}` (missing `accepted`) is a failed attempt: cursor never advances, receipt undelivered, outage recorded | `test_malformed_ok_true_sink_result_never_advances_cursor` |
| Strict sink-result typing: bool `accepted`, out-of-range/negative counts, non-bool `ok`, non-str `detail` all rejected | `test_malformed_sink_result_wrong_types_rejected` |
| One-attempt failure (`max_retries=1`) still opens the durable outage ledger | `test_one_attempt_failure_opens_outage_ledger` |
| `ok=true/accepted=0` terminal path opens the outage ledger | `test_ok_zero_accepted_opens_outage_ledger` |
| Partial-accept terminal path opens the outage ledger for the remainder | `test_partial_accept_terminal_opens_outage_for_remainder` |
| Envelope precondition enforced: unknown fields / missing base fields / Turnstone-vs-harness shape violations rejected before journal append | `test_enqueue_rejects_unknown_fields`, `test_enqueue_rejects_envelope_missing_base_fields`, `test_turnstone_originated_envelope_requires_extensions` |
| Sentinel-leak rejection: secret-shaped strings and PROMPT_TEXT/RESPONSE_TEXT/FILE_CONTENT sentinels in any field rejected before journal append | `test_enqueue_rejects_secret_shaped_and_content_sentinels` |
| Unicode default-ignorable obfuscation (e.g. `PROM<U+200B>PT_TEXT`) cannot defeat the sentinel/secret scan — stripped before matching | `test_enqueue_rejects_unicode_obfuscated_sentinel` |
| `repo` enum (only "turnstone" or null) enforced before journal append | `test_enqueue_rejects_invalid_repo_enum` |
| Complete pinned v1.0.0 schema semantics: enqueue agrees with independent jsonschema Draft-2020-12 validation on a valid/invalid corpus | `test_enqueue_full_jsonschema_enforcement` |
| Full validation is MANDATORY and fail-closed: a missing/unreadable pinned schema copy or an unusable `jsonschema` package REJECTS the event (never silently accepts) | `test_enqueue_fails_closed_when_pinned_schema_missing_or_unreadable`, `test_enqueue_fails_closed_when_jsonschema_unusable` |
| Second failed flush after newly queued work durably UPDATES the open outage episode (same episode_id, current pending set) | `test_second_failed_flush_updates_open_episode` |
| Pinned schema drift guard: embedded shape pinned to the ratified R-OBS-00 v1.0.0 schema copy (sha256); shipped runtime copy byte-identical — and byte-stable through `git archive`/checkout via `.gitattributes` (`-text`) | `test_pinned_schema_hash_matches` |
| Double-env isolation: journal lands in the disposable pinned roots; deterministic interception proof across all open primitives (builtins/io/os.open + pathlib seams), no live real-home state read | `test_transport_journal_isolated_from_real_home` + `test_conftest_pins_ambient_telemetry_home` (tests/test_isolation.py) |
| `/telemetry-status` human + machine-readable (`json` arg) output with the pinned count keys | `test_handle_status_human_and_json` |
| Store lives under disposable HERMES_TELEMETRY_HOME/HERMES_HOME (never real `~/.hermes`) | `test_transport_store_lives_under_hermes_home` + conftest autouse fixture |
| Concurrent enqueue: unique event_ids and journal offsets, complete drain | `test_concurrent_enqueue_offsets_unique_and_complete` |

## Status surface

`/telemetry-status` (human-readable) or `/telemetry-status json` (machine
record). Pinned count keys: `journaled`, `delivered`, `pending`,
`oldest_pending_age_seconds`, `last_success_at`; plus `cursor_offset`,
`torn_truncations`, `outage_open`, `sink_configured`, `transport_dir`.

With no sink configured the transport is fail-closed by construction: events
accumulate as pending (visibly, with staleness) and are never lost.

## What this module does NOT do

- No network I/O (sinks own all network behavior; tests use local/mock sinks).
- No redaction (B1 owns it; redaction runs before enqueue).
- No budget checks on the transport critical path (Trevor ruling: budget
  features default OFF and never gate enqueue/delivery).
- No exporter implementations (B4 implements the pinned `Sink` interface).
