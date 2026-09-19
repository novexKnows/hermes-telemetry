"""Tests for the durable event transport (HTB-B3).

Covers every row of the outage/staleness matrix in
docs/telemetry/transport-failclosed.md. Isolation is double-pinned: the
autouse conftest fixture AND the ``double_isolation`` fixture below both set
HERMES_HOME and HERMES_TELEMETRY_HOME to disposable tmp homes — regression
guard for the A2 isolation incident (t_c8fca739): paths.py gives
HERMES_TELEMETRY_HOME priority over HERMES_HOME, so a test overriding only
HERMES_HOME while an ambient HERMES_TELEMETRY_HOME leaks through writes into
the REAL telemetry DB — pin, never delete.
Transports are additionally pointed at explicit tmp dirs where multiple
instances share a store.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hermes_telemetry import paths as paths_mod
from hermes_telemetry import transport as tmod
from hermes_telemetry.transport import LocalFileSink, MockSink, Transport

# ---------------------------------------------------------------------------
# Isolation (A2 incident regression guard)
#
# The two module-level captures below run at IMPORT time, i.e. BEFORE the
# autouse fixtures pin HOME/USERPROFILE/HERMES_* to tmp dirs — so they capture
# what the developer's REAL telemetry home is on this machine. The isolation
# regression test snapshots it and proves the transport never writes there.
# ---------------------------------------------------------------------------

_REAL_TELEMETRY_HOME_NO_ENV = (
    Path(
        os.environ.get("HERMES_TELEMETRY_HOME")
        or os.environ.get("HERMES_HOME")
        or (Path.home() / ".hermes")
    )
    / "telemetry"
)
_REAL_HOME_PROBE_SNAPSHOT: dict[Path, tuple[bool, int | None, int | None]] = {
    p: (
        p.exists(),
        p.stat().st_mtime_ns if p.exists() else 0,
        p.stat().st_size if p.exists() else 0,
    )
    for p in (
        _REAL_TELEMETRY_HOME_NO_ENV / "telemetry.db",
        _REAL_TELEMETRY_HOME_NO_ENV / "telemetry.log",
    )
}

# ---------------------------------------------------------------------------
# Isolation (A2 incident regression guard)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def double_isolation(tmp_path, monkeypatch):
    """Every B3 test pins BOTH HERMES_HOME and HERMES_TELEMETRY_HOME to the
    disposable tmp home. The conftest autouse fixture pins both as well; a
    second pin here means an ambient value re-set later (or a missed conftest
    layer) can never outrank the disposable roots via paths.py — never rely on
    deletion alone."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "telemetry-home"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_envelope(task_id="t_2731ee50", run_id=1, trace="trc_test") -> dict:
    return {
        "board": "sovav-hermes",
        "task_id": task_id,
        "run_id": run_id,
        "parent_task_ids": [],
        "profile/role": "worker",
        "model": "z-ai/glm-5.3-flash",
        "provider": "openrouter",
        "key_id": "label:worker-openrouter-a",
        "call_class": "primary",
        "handoff_id": None,
        "worktree/branch": "wt/htb-b3",
        "trace_id": trace,
    }


class SleepRecorder:
    """Replaces time.sleep in the transport: records backoff delays instead."""

    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def failing_sink(detail="connection refused"):
    return MockSink(lambda events, calls: {"ok": False, "accepted": 0, "detail": detail})


def timeout_sink(msg="sink timed out"):
    def _behavior(events, calls):
        raise TimeoutError(msg)

    return MockSink(_behavior)


def make_transport(sink=None, tmp_path=None, **kwargs) -> Transport:
    kwargs.setdefault("base_delay_s", 0.001)
    kwargs.setdefault("max_delay_s", 0.004)
    sleep_rec = SleepRecorder()
    kwargs.setdefault("sleep", sleep_rec)
    if tmp_path is not None:
        kwargs.setdefault("dir", tmp_path / "transport")
    t = Transport(sink, **kwargs)
    return t


# ---------------------------------------------------------------------------
# Core fail-closed behavior
# ---------------------------------------------------------------------------


def test_enqueue_returns_durable_receipt(tmp_path):
    t = make_transport(failing_sink(), tmp_path)
    receipt = t.enqueue(make_envelope())
    assert receipt["event_id"].startswith("evt_")
    assert isinstance(receipt["journal_offset"], int)
    # The journal line exists on disk before any sink attempt.
    raw = (tmp_path / "transport" / "events.jsonl").read_text(encoding="utf-8")
    record = json.loads(raw.splitlines()[0])
    assert record["event_id"] == receipt["event_id"]
    assert record["envelope"]["task_id"] == "t_2731ee50"


def test_journal_survives_sink_down(tmp_path, caplog):
    """Matrix row: sink unavailable — events stay pending, nothing lost."""
    t = make_transport(failing_sink(), tmp_path, max_retries=2)
    receipts = [t.enqueue(make_envelope(trace=f"trc_{i}")) for i in range(3)]
    summary = t.flush()
    assert summary["ok"] is False
    assert summary["pending"] == 3
    # Receipts remain queryable while the sink is down.
    for r in receipts:
        got = t.receipt(r["event_id"])
        assert got is not None
        assert got["delivered"] is False
    status = t.status()
    assert status["journaled"] == 3 and status["pending"] == 3 and status["delivered"] == 0
    assert status["outage_open"] is True
    # Outage ledger recorded the undelivered events.
    ledger = (tmp_path / "transport" / "outage_ledger.jsonl").read_text(encoding="utf-8")
    entry = json.loads(ledger.splitlines()[-1])
    assert entry["event"] == "open"
    assert entry["pending_count"] == 3
    assert entry["pending_event_ids"] == [r["event_id"] for r in receipts]
    # Restart with a healthy sink: everything drains, zero loss.
    delivered_path = tmp_path / "delivered.jsonl"
    t2 = make_transport(LocalFileSink(delivered_path), tmp_path)
    summary2 = t2.flush()
    assert summary2["ok"] is True and summary2["pending"] == 0
    lines = delivered_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    traces = [json.loads(line)["envelope"]["trace_id"] for line in lines]
    assert traces == ["trc_0", "trc_1", "trc_2"]  # FIFO order preserved


def test_sink_timeout_retries_with_exponential_backoff(tmp_path):
    """Matrix row: sink slow/timeout — bounded retries, exponential backoff,
    outage ledger entry, events stay pending."""
    sink = timeout_sink()
    t = make_transport(sink, tmp_path, max_retries=4)
    t.enqueue(make_envelope())
    summary = t.flush()
    assert summary["ok"] is False
    assert summary["attempts"] == 4
    # Exponential backoff sequence: base * 2^n, capped at max_delay.
    assert t._sleep.delays == [0.001, 0.002, 0.004]
    assert t.status()["pending"] == 1
    ledger_line = (
        (tmp_path / "transport" / "outage_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    entry = json.loads(ledger_line)
    assert "TimeoutError" in entry["detail"]


def test_partial_accept_requeues_only_unaccepted(tmp_path):
    """Matrix row: partial accept — cursor advances only over the accepted
    prefix; exactly the unaccepted remainder is redelivered."""
    sink = MockSink(lambda events, calls: {"ok": True, "accepted": 2, "detail": "partial"})
    t = make_transport(sink, tmp_path)
    for i in range(5):
        t.enqueue(make_envelope(trace=f"trc_{i}"))
    summary = t.flush()
    assert summary["delivered"] == 2 and summary["pending"] == 3
    # First batch contained all 5 events; only the first 2 were accepted.
    assert len(sink.calls) == 1
    assert [e["envelope"]["trace_id"] for e in sink.calls[0][:2]] == ["trc_0", "trc_1"]
    # Second flush redelivers exactly the 3 unaccepted events.
    healthy = MockSink()
    t.sink = healthy
    summary2 = t.flush()
    assert summary2["delivered"] == 3 and summary2["pending"] == 0
    got = [e["envelope"]["trace_id"] for e in healthy.calls[0]]
    assert got == ["trc_2", "trc_3", "trc_4"]


def test_crash_mid_batch_resumes_without_duplication(tmp_path):
    """Matrix row: crash mid-batch — cursor rewind (lost state) forces
    redelivery; the idempotent-by-event_id sink dedupes so the delivered
    file holds each event exactly once."""
    delivered_path = tmp_path / "delivered.jsonl"
    sink = LocalFileSink(delivered_path)
    t = make_transport(sink, tmp_path)
    ids = [t.enqueue(make_envelope(trace=f"trc_{i}"))["event_id"] for i in range(5)]
    t.flush()
    assert len(delivered_path.read_text(encoding="utf-8").splitlines()) == 5
    # Simulate a crash after the sink accepted but before the cursor persisted:
    # rewind the persisted cursor so a restart sees all 5 as pending.
    state_path = tmp_path / "transport" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["cursor_offset"] = 0
    state["delivered_count"] = 0
    state_path.write_text(json.dumps(state), encoding="utf-8")
    # Process restart: a fresh Transport over the same store.
    t2 = make_transport(LocalFileSink(delivered_path), tmp_path)
    summary = t2.flush()
    assert summary["ok"] is True and summary["pending"] == 0
    # No duplication: each event_id appears exactly once in the sink file.
    delivered_ids = [
        json.loads(line)["event_id"]
        for line in delivered_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(delivered_ids) == 5 and set(delivered_ids) == set(ids)
    assert t2.status()["delivered"] == 5


def test_process_restart_drains_without_loss(tmp_path):
    """Matrix row: process restart — new Transport instance over the same
    journal resumes and drains fully."""
    t = make_transport(failing_sink(), tmp_path)
    for i in range(4):
        t.enqueue(make_envelope(trace=f"trc_{i}"))
    t.flush()
    del t
    delivered_path = tmp_path / "delivered.jsonl"
    t2 = make_transport(LocalFileSink(delivered_path), tmp_path)
    assert t2.status()["pending"] == 4
    t2.flush()
    lines = delivered_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["envelope"]["trace_id"] for line in lines] == [
        f"trc_{i}" for i in range(4)
    ]
    assert t2.status()["delivered"] == 4


def test_staleness_surface_reports_oldest_pending_age(tmp_path, monkeypatch):
    """Matrix row: stale/aged events — oldest_pending_age_seconds reflects the
    oldest pending event and resets to 0 once drained."""
    real_now = time.time()
    backdate = {"seconds": 0.0}

    def fake_utcnow():
        return datetime.fromtimestamp(real_now - backdate["seconds"], tz=timezone.utc).isoformat()

    monkeypatch.setattr(tmod, "_utcnow_iso", fake_utcnow)
    t = make_transport(failing_sink(), tmp_path)
    backdate["seconds"] = 120.0
    t.enqueue(make_envelope(trace="trc_old"))
    backdate["seconds"] = 60.0
    t.enqueue(make_envelope(trace="trc_new"))
    status = t.status()
    assert status["pending"] == 2
    assert 118.0 <= status["oldest_pending_age_seconds"] <= 122.0
    # Drain: staleness goes to zero, last_success_at is set.
    t.sink = MockSink()
    summary = t.flush()
    assert summary["ok"] is True
    status2 = t.status()
    assert status2["pending"] == 0
    assert status2["oldest_pending_age_seconds"] == 0.0
    assert status2["last_success_at"] is not None


def test_journal_corruption_torn_last_line(tmp_path, caplog):
    """Matrix row: journal corruption on last line — torn tail is truncated
    with a warning; earlier records are never silently dropped."""
    t = make_transport(failing_sink(), tmp_path)
    ids = [t.enqueue(make_envelope(trace=f"trc_{i}"))["event_id"] for i in range(3)]
    # Simulate a process killed mid-append: a partial JSON line with no newline.
    with open(tmp_path / "transport" / "events.jsonl", "ab") as fh:
        fh.write(b'{"event_id": "evt_torn", "enqueu')
    # Restart: the torn line is truncated with a warning, earlier records intact.
    t2 = make_transport(LocalFileSink(tmp_path / "delivered.jsonl"), tmp_path)
    assert any("torn" in rec.message.lower() for rec in caplog.records)
    assert t2.status()["torn_truncations"] == 1
    assert t2.status()["journaled"] == 3
    for eid in ids:
        got = t2.receipt(eid)
        assert got is not None and got["delivered"] is False
    summary = t2.flush()
    assert summary["ok"] is True
    lines = (tmp_path / "delivered.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


# ---------------------------------------------------------------------------
# Fail-closed queryability while the sink never returns
# ---------------------------------------------------------------------------


def test_receipts_queryable_while_sink_never_returns(tmp_path):
    """Matrix row: sink never returns — flush blocks, but receipts and status
    remain queryable from another thread; the journal is the durability floor."""
    release = threading.Event()

    class NeverReturningSink(tmod.Sink):
        def deliver(self, events):
            release.wait(timeout=10.0)
            return {"ok": False, "accepted": 0, "detail": "cancelled"}

    t = make_transport(NeverReturningSink(), tmp_path, max_retries=2)
    receipts = [t.enqueue(make_envelope(trace=f"trc_{i}")) for i in range(2)]
    flusher = threading.Thread(target=t.flush, daemon=True)
    flusher.start()
    try:
        # While the sink is blocked, the fail-closed surfaces still answer.
        for r in receipts:
            got = t.receipt(r["event_id"])
            assert got is not None and got["delivered"] is False
        status = t.status()
        assert status["journaled"] == 2 and status["pending"] == 2
        assert status["oldest_pending_age_seconds"] >= 0.0
    finally:
        release.set()
        flusher.join(timeout=15.0)
    assert not flusher.is_alive()
    assert t.status()["pending"] == 2  # still durably journaled, nothing lost


# ---------------------------------------------------------------------------
# Sink interface conformance
# ---------------------------------------------------------------------------


def test_local_file_sink_conformance_and_dedupe(tmp_path):
    sink = LocalFileSink(tmp_path / "out.jsonl")
    events = [
        {
            "event_id": "evt_a",
            "enqueued_at": "2026-01-01T00:00:00+00:00",
            "attempt": 1,
            "envelope": make_envelope(trace="trc_a"),
        },
        {
            "event_id": "evt_b",
            "enqueued_at": "2026-01-01T00:00:01+00:00",
            "attempt": 1,
            "envelope": make_envelope(trace="trc_b"),
        },
    ]
    result = sink.deliver(events)
    assert result["ok"] is True and result["accepted"] == 2 and "detail" in result
    # Redeliver one duplicate + one new: duplicate counts as accepted, not rewritten.
    dup = dict(events[0])
    new = {
        "event_id": "evt_c",
        "enqueued_at": "2026-01-01T00:00:02+00:00",
        "attempt": 2,
        "envelope": make_envelope(trace="trc_c"),
    }
    result2 = sink.deliver([dup, new])
    assert result2["ok"] is True and result2["accepted"] == 2
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event_id"] for line in lines] == ["evt_a", "evt_b", "evt_c"]


def test_no_sink_configured_is_fail_closed_not_fatal(tmp_path):
    t = make_transport(None, tmp_path)
    t.enqueue(make_envelope())
    summary = t.flush()
    assert summary["ok"] is False and summary["pending"] == 1
    assert "no sink" in summary["detail"]
    assert t.status()["sink_configured"] is False
    assert t.receipt(t._journal_records[0].event_id) is not None


def test_non_conforming_sink_result_treated_as_failure(tmp_path):
    sink = MockSink(lambda events, calls: {"accepted": 99})  # missing ok bool
    t = make_transport(sink, tmp_path, max_retries=1)
    t.enqueue(make_envelope())
    summary = t.flush()
    assert summary["ok"] is False and summary["pending"] == 1
    assert "non-conforming sink result" in summary["detail"]
    # Terminal path: even a one-attempt failure must open the outage ledger.
    assert t.status()["outage_open"] is True


def test_malformed_ok_true_sink_result_never_advances_cursor(tmp_path):
    """BLOCKING regression: a malformed sink result ``{"ok": true}`` (missing
    ``accepted``) must NOT be treated as full acceptance. Nothing is persisted
    at the sink, so the cursor must not advance: pending stays > 0 and the
    receipt stays undelivered."""
    sink = MockSink(lambda events, calls: {"ok": True})  # no accepted, no detail
    t = make_transport(sink, tmp_path, max_retries=1)
    receipt = t.enqueue(make_envelope(trace="trc_leak"))
    summary = t.flush()
    assert summary["ok"] is False and summary["pending"] == 1
    assert "non-conforming sink result" in summary["detail"]
    got = t.receipt(receipt["event_id"])
    assert got is not None and got["delivered"] is False  # cursor NOT advanced
    assert t.status()["pending"] == 1 and t.status()["delivered"] == 0
    # Failed attempt -> durable outage evidence, not a silent full acceptance.
    assert t.status()["outage_open"] is True
    ledger = (tmp_path / "transport" / "outage_ledger.jsonl").read_text(encoding="utf-8")
    assert json.loads(ledger.splitlines()[-1])["pending_count"] == 1


def test_malformed_sink_result_wrong_types_rejected(tmp_path):
    """accepted must be a real int (bool is not), in 0..len(batch); detail must
    be str; ok must be exactly bool. Every wrong-typed result is a failed
    attempt and the cursor never advances."""
    t = make_transport(failing_sink(), tmp_path, max_retries=1)
    t.enqueue(make_envelope())
    cases = [
        {"ok": True, "accepted": True, "detail": "bool count"},
        {"ok": True, "accepted": 2, "detail": "beyond batch"},  # batch of 1
        {"ok": True, "accepted": -1, "detail": "negative"},
        {"ok": "yes", "accepted": 1, "detail": "ok not bool"},
        {"ok": True, "accepted": 1, "detail": 7},
    ]
    for result in cases:
        t.sink = MockSink(lambda events, calls, _r=result: _r)
        summary = t.flush(max_retries=1)
        assert summary["ok"] is False, f"case {result!r} must be a failed attempt"
        assert t.status()["pending"] == 1, f"case {result!r} must not advance the cursor"


# ---------------------------------------------------------------------------
# Outage ledger terminal paths (EVERY flush ending with pending records)
# ---------------------------------------------------------------------------


def test_one_attempt_failure_opens_outage_ledger(tmp_path):
    """Blocking regression: a max_retries=1 failure must NOT exit before the
    outage ledger opens — pending=1 with outage_open=true, ledger on disk."""
    t = make_transport(failing_sink(), tmp_path, max_retries=1)
    t.enqueue(make_envelope())
    summary = t.flush()
    assert summary["ok"] is False and summary["pending"] == 1
    status = t.status()
    assert status["outage_open"] is True
    ledger_path = tmp_path / "transport" / "outage_ledger.jsonl"
    assert ledger_path.exists()
    entry = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["event"] == "open" and entry["pending_count"] == 1


def test_ok_zero_accepted_opens_outage_ledger(tmp_path):
    """Terminal path: ok=true / accepted=0 ends the flush with pending records
    and must durably open the outage ledger."""
    sink = MockSink(lambda events, calls: {"ok": True, "accepted": 0, "detail": "buffering"})
    t = make_transport(sink, tmp_path, max_retries=1)
    t.enqueue(make_envelope())
    summary = t.flush()
    assert summary["ok"] is False and summary["pending"] == 1
    assert t.status()["outage_open"] is True
    ledger_path = tmp_path / "transport" / "outage_ledger.jsonl"
    entry = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["event"] == "open" and entry["pending_count"] == 1


def test_partial_accept_terminal_opens_outage_for_remainder(tmp_path):
    """Terminal path: a flush that ends via partial accept leaves the remainder
    pending and MUST durably open the outage ledger for it."""
    sink = MockSink(lambda events, calls: {"ok": True, "accepted": 1, "detail": "partial"})
    t = make_transport(sink, tmp_path)
    for i in range(3):
        t.enqueue(make_envelope(trace=f"trc_{i}"))
    summary = t.flush()
    assert summary["delivered"] == 1 and summary["pending"] == 2
    assert t.status()["outage_open"] is True
    ledger_path = tmp_path / "transport" / "outage_ledger.jsonl"
    entry = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["event"] == "open" and entry["pending_count"] == 2


# ---------------------------------------------------------------------------
# Envelope metadata-only boundary (redaction precondition, ENFORCED)
# ---------------------------------------------------------------------------


def test_enqueue_rejects_non_dict(tmp_path):
    t = make_transport(failing_sink(), tmp_path)
    with pytest.raises(TypeError):
        t.enqueue("not-an-envelope")


def test_enqueue_rejects_envelope_missing_base_fields(tmp_path):
    """The redaction/schema precondition is enforced: an envelope missing
    required base fields violates the pinned v1.0.0 shape and is REJECTED
    before journal append (not merely warned about)."""
    t = make_transport(failing_sink(), tmp_path)
    with pytest.raises(ValueError, match="before journal append"):
        t.enqueue({"task_id": "t_x"})
    assert t.status()["journaled"] == 0  # nothing was journaled


def test_enqueue_rejects_unknown_fields(tmp_path):
    envelope = make_envelope()
    envelope["prompt"] = "you must never see this"  # not in the pinned schema
    t = make_transport(failing_sink(), tmp_path)
    with pytest.raises(ValueError, match="unknown field"):
        t.enqueue(envelope)
    assert t.status()["journaled"] == 0


def test_enqueue_rejects_secret_shaped_and_content_sentinels(tmp_path):
    """Sentinel-leak rejection: banned marker content (secret-shaped strings,
    prompt/response/file content) in ANY field is rejected before journal
    append — nothing is written to the journal."""
    t = make_transport(failing_sink(), tmp_path)
    leaks = {
        "api key in key_id": ("key_id", "label:sk-proj-abcdef1234567890abcdef"),
        "github token in model": ("model", "ghp_" + "a1B2c3D4e5F6g7H8" * 4),
        "prompt text in board": ("board", "PROMPT_TEXT: you are a helpful assistant"),
        "response text in trace": ("trace_id", "RESPONSE_TEXT: leaked answer"),
        "file content in profile": ("profile/role", "FILE_CONTENT: -----BEGIN PRIVATE KEY-----"),
    }
    for name, (field, value) in leaks.items():
        envelope = make_envelope()
        envelope[field] = value
        with pytest.raises(ValueError) as exc_info:
            t.enqueue(envelope)
        assert "before journal append" in str(exc_info.value), name
    assert t.status()["journaled"] == 0
    journal = t.dir / "events.jsonl"
    assert not journal.exists() or journal.read_text(encoding="utf-8") == ""


def test_turnstone_originated_envelope_requires_extensions(tmp_path):
    t = make_transport(failing_sink(), tmp_path)
    bad = make_envelope()
    bad["repo"] = "turnstone"  # extension fields required on Turnstone events
    with pytest.raises(ValueError, match="Turnstone event"):
        t.enqueue(bad)
    good = dict(bad)
    for req in tmod._TURNSTONE_REQUIRED:
        good[req] = "dev" if req == "runtime_environment" else f"val_{req}"
    receipt = t.enqueue(good)  # conforms: journaled
    assert receipt["event_id"].startswith("evt_")


def test_pinned_schema_hash_matches():
    """Drift guard: the embedded envelope-shape boundary is pinned to the
    ratified R-OBS-00 v1.0.0 schema copy (sha256 recorded at pin time)."""
    import hashlib

    pinned = Path(tmod.PINNED_SCHEMA_PATH)
    if not pinned.exists():
        pytest.skip(f"pinned schema copy not present on this machine: {pinned}")
    digest = hashlib.sha256(pinned.read_bytes()).hexdigest()
    assert digest == tmod.PINNED_SCHEMA_SHA256
    schema = json.loads(pinned.read_text(encoding="utf-8"))
    assert schema["version"] == "1.0.0" and schema["additionalProperties"] is False
    assert set(schema["required"]) == set(tmod.ENVELOPE_BASE_FIELDS)
    assert set(schema["properties"]) == set(tmod.ENVELOPE_SCHEMA_FIELDS)
    assert set(schema["properties"]["call_class"]["enum"]) == set(tmod.ENVELOPE_CALL_CLASSES)


# ---------------------------------------------------------------------------
# Envelope precondition + command surface
# ---------------------------------------------------------------------------


def test_handle_status_human_and_json(tmp_path):
    t = make_transport(failing_sink(), None)  # default dir: HERMES_HOME/telemetry/transport
    t.enqueue(make_envelope())
    human = tmod.handle_status("")
    assert "journaled:" in human and "pending:" in human
    assert "nothing was lost" in human
    record = json.loads(tmod.handle_status("json"))
    for key in (
        "journaled",
        "delivered",
        "pending",
        "oldest_pending_age_seconds",
        "last_success_at",
    ):
        assert key in record


def test_transport_store_lives_under_hermes_home(tmp_path):
    """Both env pins come from the double_isolation fixture (conftest layer
    pins them too); the store must resolve under the disposable telemetry
    home — never the developer's real ~/.hermes."""
    t = make_transport(failing_sink(), None)  # default dir resolution
    pinned_home = Path(paths_mod.get_telemetry_home())
    assert str(t.dir).startswith(str(pinned_home))
    t.enqueue(make_envelope())
    assert (t.dir / "events.jsonl").exists()


def test_transport_journal_isolated_from_real_home(tmp_path):
    """TM isolation directive, transport edition: with BOTH HERMES_HOME and
    HERMES_TELEMETRY_HOME pinned to disposable roots, the journal must land
    inside the disposable root and no real-home path (the developer's real
    telemetry home / ~/.hermes) may be touched — no journal file there, and no
    modification of pre-existing real-home files by enqueue + flush."""
    t = make_transport(failing_sink(), None)  # default dir resolution
    t.enqueue(make_envelope())
    t.flush()

    # 1. The journal lands inside the disposable pinned roots — nothing else.
    assert Path(str(t.dir)).is_relative_to(tmp_path), f"journal left the disposable root: {t.dir}"
    assert (t.dir / "events.jsonl").exists()
    # 2. The REAL telemetry home (resolved with no env overrides at import
    # time, i.e. the machine's actual home) was never touched: no transport
    # journal appeared there, and pre-existing real files were not modified.
    real_tele = _REAL_TELEMETRY_HOME_NO_ENV
    real_transport = real_tele / "transport"
    assert not (real_transport / "events.jsonl").exists(), (
        f"transport journaled into the real home: {real_transport / 'events.jsonl'}"
    )
    for probe_path, (existed, mtime_ns, size) in _REAL_HOME_PROBE_SNAPSHOT.items():
        assert probe_path.exists() == existed, f"real-home file created during test: {probe_path}"
        if existed:
            assert probe_path.stat().st_mtime_ns == mtime_ns and (
                probe_path.stat().st_size == size
            ), f"real-home file modified during test: {probe_path}"
    # 3. The pin target really is inside the disposable tmp root.
    assert Path(str(t.dir)).is_relative_to(tmp_path)
    assert paths_mod.get_transport_dir().is_relative_to(tmp_path)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_enqueue_offsets_unique_and_complete(tmp_path):
    t = make_transport(failing_sink(), tmp_path)
    receipts: list[dict] = []
    lock = threading.Lock()

    def worker(n):
        for i in range(10):
            r = t.enqueue(make_envelope(trace=f"trc_{n}_{i}"))
            with lock:
                receipts.append(r)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(receipts) == 40
    assert len({r["event_id"] for r in receipts}) == 40
    assert len({r["journal_offset"] for r in receipts}) == 40
    assert t.status()["journaled"] == 40
    t.sink = MockSink()
    summary = t.flush()
    assert summary["ok"] is True and summary["delivered"] == 40
