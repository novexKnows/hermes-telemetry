# Durable event transport — fail-closed outage/staleness matrix (HTB-B3)

Scope: `transport.py` (durable local queue for ratified R-OBS-00 envelopes),
the pinned sink interface consumed by B4 exporters, the outage ledger, and the
`/telemetry-status` surface. Envelope schema and redaction are owned by B1
(R-OBS-00 v1.0.0); the transport receives **already-redacted envelope dicts**
and asserts this as a precondition — it never redacts, never drops.

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
   successful flush closes the episode. No outage is ever silent.
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
duplicate `event_id` counts as accepted and is not re-written. Timeout
enforcement is the sink's responsibility (B4 owns exporter HTTP timeouts); the
transport treats any exception or non-conforming result as a failed attempt
and retries with exponential backoff (`base * 2^n`, capped).

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
| Non-conforming sink result (missing `ok`) treated as failed attempt, not crash | `test_non_conforming_sink_result_treated_as_failure` |
| Non-dict envelope rejected; envelope missing base fields journaled anyway with a warning (fail-closed) | `test_enqueue_rejects_non_dict_and_warns_on_missing_fields` |
| `/telemetry-status` human + machine-readable (`json` arg) output with the pinned count keys | `test_handle_status_human_and_json` |
| Store lives under disposable HERMES_HOME (never real `~/.hermes`) | `test_transport_store_lives_under_hermes_home` + conftest autouse fixture |
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
