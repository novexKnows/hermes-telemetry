"""Durable local event transport for R-OBS-00 correlation envelopes.

Fail-closed design (TM-pinned, HTB-B3):

1. ``enqueue(envelope)`` appends the envelope to an append-only JSONL journal
   under ``<telemetry home>/transport/events.jsonl`` and fsyncs BEFORE any sink
   is touched. The receipt ``{event_id, journal_offset}`` is therefore always
   queryable, even when the sink never returns.
2. ``flush()`` delivers pending journal records to a sink in bounded batches,
   with bounded retries and exponential backoff. A persistent cursor in
   ``state.json`` marks how much of the journal has been accepted; a crash
   mid-batch resumes from the cursor without loss (journal + cursor) and
   without effective duplication (redelivered records carry a stable
   ``event_id``; sinks dedupe on it — see ``LocalFileSink``).
3. Every failed delivery episode is recorded in ``outage_ledger.jsonl`` with
   the undelivered event ids and the staleness (age of the oldest pending
   event). Nothing is ever dropped silently: if the sink is down, events stay
   pending and are surfaced by ``status()`` / the ``/telemetry-status`` command.

SINK INTERFACE (pinned; B4 exporters conform to this exact shape)::

    class Sink:
        def deliver(self, events: list[dict]) -> dict: ...

Each event dict passed to ``deliver`` is::

    {
        "event_id": str,        # transport-assigned, stable across retries
        "enqueued_at": str,     # ISO-8601 UTC timestamp of the original enqueue
        "attempt": int,         # 1-based delivery attempt counter
        "envelope": {...},      # the R-OBS-00 envelope, verbatim, already redacted
    }

``deliver`` returns ``{"ok": bool, "accepted": int, "detail": str}``:
``accepted`` counts events durable at the sink IN ORDER — events beyond the
accepted prefix remain pending and are retried (partial accept). A duplicate
``event_id`` (crash between sink-accept and cursor-persist) counts as accepted;
the sink dedupes it. Timeout enforcement is the sink's responsibility (B4 owns
exporter HTTP timeouts); the transport treats any exception or non-conforming
result as a failed attempt.

Envelope precondition: the transport receives ALREADY-REDACTED envelope dicts
(B1 owns redaction; redaction always runs before enqueue). ``enqueue`` asserts
the dict type and warns on missing R-OBS-00 base fields, but never drops an
event for shape reasons — fail-closed means journal first, questions later.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .paths import get_transport_dir

logger = logging.getLogger(__name__)

JOURNAL_FILE = "events.jsonl"
STATE_FILE = "state.json"
LEDGER_FILE = "outage_ledger.jsonl"
STATE_VERSION = 1

#: R-OBS-00 v1.0.0 canonical base fields (contract §1.1). Used only for a
#: warning-level structural precondition check — never for dropping events.
ENVELOPE_BASE_FIELDS = (
    "board",
    "task_id",
    "run_id",
    "parent_task_ids",
    "profile/role",
    "model",
    "provider",
    "key_id",
    "call_class",
    "handoff_id",
    "worktree/branch",
    "trace_id",
)

LEDGER_EVENT_ID_CAP = 1000  # cap event-id lists written to a ledger entry


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> float:
    """Parse an ISO-8601 timestamp to a unix epoch float (UTC)."""
    return datetime.fromisoformat(ts).timestamp()


# ---------------------------------------------------------------------------
# Sink interface
# ---------------------------------------------------------------------------


class Sink:
    """Pinned sink interface — B4 exporters implement ``deliver`` exactly."""

    def deliver(self, events: list[dict]) -> dict:
        """Deliver a batch; return ``{"ok": bool, "accepted": int, "detail": str}``."""
        raise NotImplementedError


class LocalFileSink(Sink):
    """Conformance sink: appends delivered records to a local JSONL file.

    Idempotent by ``event_id``: an event already present in the file is
    counted as accepted but not written again. This is the reference
    implementation of the dedupe behaviour every B4 exporter must provide.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen: set[str] = set()
        if self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._seen.add(json.loads(line)["event_id"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue  # tolerate torn tail lines here too

    def deliver(self, events: list[dict]) -> dict:
        written = 0
        dupes = 0
        with open(self.path, "a", encoding="utf-8") as fh:
            for event in events:
                event_id = event.get("event_id")
                if not event_id or event_id in self._seen:
                    dupes += 1
                    continue
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
                self._seen.add(event_id)
                written += 1
            fh.flush()
            os.fsync(fh.fileno())
        detail = f"local-file: wrote {written}, deduped {dupes}"
        return {"ok": True, "accepted": len(events), "detail": detail}


class MockSink(Sink):
    """Scriptable sink for tests: inject a behavior callable that receives
    ``(events, calls)`` and returns the deliver() result dict."""

    def __init__(self, behavior: Callable[[list[dict], list[list[dict]]], dict] | None = None):
        self.behavior = behavior or (
            lambda events, calls: {"ok": True, "accepted": len(events), "detail": "mock ok"}
        )
        self.calls: list[list[dict]] = []

    def deliver(self, events: list[dict]) -> dict:
        self.calls.append(list(events))
        return self.behavior(events, self.calls)


# ---------------------------------------------------------------------------
# Journal records
# ---------------------------------------------------------------------------


class _Record:
    __slots__ = ("event_id", "enqueued_at", "envelope", "start_offset", "end_offset")

    def __init__(
        self, event_id: str, enqueued_at: str, envelope: dict, start_offset: int, end_offset: int
    ):
        self.event_id = event_id
        self.enqueued_at = enqueued_at
        self.envelope = envelope
        self.start_offset = start_offset
        self.end_offset = end_offset


def _read_records(path: Path, from_offset: int = 0, limit: int | None = None) -> list[_Record]:
    """Parse complete JSONL records starting at ``from_offset``. A torn final
    line (partial write) is skipped by the caller's repair step, not here."""
    records: list[_Record] = []
    if not path.exists():
        return records
    with open(path, "rb") as fh:
        fh.seek(from_offset)
        offset = from_offset
        for raw in fh:
            start = offset
            offset += len(raw)
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
                records.append(
                    _Record(obj["event_id"], obj["enqueued_at"], obj["envelope"], start, offset)
                )
            except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
                # Only the LAST line can be torn (append-only, whole-line writes).
                if offset >= path.stat().st_size:
                    break
                raise
            if limit is not None and len(records) >= limit:
                break
    return records


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport:
    """Durable local queue for R-OBS-00 envelopes with fail-closed semantics."""

    def __init__(
        self,
        sink: Sink | None = None,
        *,
        max_retries: int = 5,
        base_delay_s: float = 0.05,
        max_delay_s: float = 2.0,
        batch_size: int = 100,
        dir: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.dir = Path(dir) if dir is not None else get_transport_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.dir / JOURNAL_FILE
        self.state_path = self.dir / STATE_FILE
        self.ledger_path = self.dir / LEDGER_FILE
        self.sink = sink
        self.max_retries = max(1, int(max_retries))
        self.base_delay_s = float(base_delay_s)
        self.max_delay_s = float(max_delay_s)
        self.batch_size = max(1, int(batch_size))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._repair_journal()
        self._journal_records = _read_records(self.journal_path, 0)
        self._journaled = len(self._journal_records)
        self._persist_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _default_state(self) -> dict:
        return {
            "version": STATE_VERSION,
            "cursor_offset": 0,
            "journal_size": 0,
            "delivered_count": 0,
            "last_success_at": None,
            "torn_truncations": 0,
            "open_episode": None,
        }

    def _load_state(self) -> dict:
        state = self._default_state()
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update(loaded)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "transport: unreadable state.json (%s) — starting from defaults; "
                    "journal remains authoritative, events may be redelivered",
                    exc,
                )
        return state

    def _persist_state(self) -> None:
        with self._lock:
            tmp = self.state_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------
    # Journal integrity
    # ------------------------------------------------------------------

    def _repair_journal(self) -> None:
        """Tolerate a torn final line: truncate it (with a warning), never
        touch earlier complete records. Uses journal_size from state to only
        validate the unvalidated tail; falls back to a full scan when state is
        missing."""
        if not self.journal_path.exists():
            return
        trusted = self._state.get("journal_size", 0)
        size = self.journal_path.stat().st_size
        check_from = trusted if 0 <= trusted <= size else 0
        good_end = check_from
        with open(self.journal_path, "rb") as fh:
            fh.seek(check_from)
            offset = check_from
            for raw in fh:
                offset += len(raw)
                if not raw.strip():
                    continue
                try:
                    json.loads(raw)
                    good_end = offset
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
        if good_end < size:
            # Torn final line (or file shrank below the trusted offset):
            # truncate at the last complete record; earlier records untouched.
            with open(self.journal_path, "r+b") as fh:
                fh.truncate(good_end)
            self._state["torn_truncations"] = int(self._state.get("torn_truncations", 0)) + 1
            logger.warning(
                "transport: torn final journal line truncated at byte %d "
                "(torn_truncations=%d); earlier records intact",
                good_end,
                self._state["torn_truncations"],
            )
        self._state["journal_size"] = good_end

    # ------------------------------------------------------------------
    # Enqueue (fail-closed: journal + fsync BEFORE any sink attempt)
    # ------------------------------------------------------------------

    def enqueue(self, envelope: dict) -> dict:
        """Durably journal an already-redacted envelope; return the receipt."""
        if not isinstance(envelope, dict):
            raise TypeError("enqueue expects an envelope dict (redaction runs before enqueue)")
        missing = [f for f in ENVELOPE_BASE_FIELDS if f not in envelope]
        if missing:
            logger.warning(
                "transport: envelope missing base field(s) %s — journaling anyway "
                "(fail-closed; fix at the emitter)",
                missing,
            )
        event_id = "evt_" + uuid.uuid4().hex
        enqueued_at = _utcnow_iso()
        record = {"event_id": event_id, "enqueued_at": enqueued_at, "envelope": envelope}
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            offset = self.journal_path.stat().st_size if self.journal_path.exists() else 0
            with open(self.journal_path, "ab") as fh:
                fh.write(line.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            self._state["journal_size"] = offset + len(line.encode("utf-8"))
            self._persist_state()
            self._journaled += 1
            self._journal_records.append(
                _Record(event_id, enqueued_at, envelope, offset, self._state["journal_size"])
            )
        return {"event_id": event_id, "journal_offset": offset}

    def receipt(self, event_id: str) -> dict | None:
        """Query a journaled receipt — always available, sink or no sink."""
        with self._lock:
            for rec in self._journal_records:
                if rec.event_id == event_id:
                    return {
                        "event_id": rec.event_id,
                        "journal_offset": rec.start_offset,
                        "enqueued_at": rec.enqueued_at,
                        "delivered": rec.end_offset <= self._state["cursor_offset"],
                    }
        return None

    # ------------------------------------------------------------------
    # Delivery loop (bounded retries, exponential backoff)
    # ------------------------------------------------------------------

    def _attempt_deliver(self, batch: list[_Record], attempt: int) -> dict:
        """One sink attempt. Returns a normalized result dict; never raises."""
        if self.sink is None:
            return {
                "ok": False,
                "accepted": 0,
                "detail": "no sink configured (events stay pending)",
            }
        payload = [
            {
                "event_id": r.event_id,
                "enqueued_at": r.enqueued_at,
                "attempt": attempt,
                "envelope": r.envelope,
            }
            for r in batch
        ]
        try:
            result = self.sink.deliver(payload)
        except Exception as exc:  # noqa: BLE001 — fail-closed: any sink failure is an outage
            return {"ok": False, "accepted": 0, "detail": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            return {"ok": False, "accepted": 0, "detail": f"non-conforming sink result: {result!r}"}
        accepted = result.get("accepted", len(batch) if result["ok"] else 0)
        if not isinstance(accepted, int) or accepted < 0:
            return {"ok": False, "accepted": 0, "detail": f"invalid accepted count: {accepted!r}"}
        accepted = min(accepted, len(batch))
        return {
            "ok": bool(result["ok"]),
            "accepted": accepted,
            "detail": str(result.get("detail", "")),
        }

    def _oldest_pending_age(self) -> float:
        pending = self._journaled - int(self._state["delivered_count"])
        if pending <= 0:
            return 0.0
        cursor = int(self._state["cursor_offset"])
        with self._lock:
            for rec in self._journal_records:
                if rec.start_offset >= cursor:
                    try:
                        return max(0.0, self._clock() - _parse_iso(rec.enqueued_at))
                    except ValueError:
                        return 0.0
        return 0.0

    def _open_outage(self, kind: str, detail: str) -> str:
        episode_id = "ep_" + uuid.uuid4().hex[:12]
        with self._lock:
            pending_ids = [
                rec.event_id
                for rec in self._journal_records
                if rec.start_offset >= int(self._state["cursor_offset"])
            ]
            entry = {
                "event": "open",
                "episode_id": episode_id,
                "ts": _utcnow_iso(),
                "kind": kind,
                "detail": detail,
                "pending_count": len(pending_ids),
                "pending_event_ids": pending_ids[:LEDGER_EVENT_ID_CAP],
                "pending_event_ids_truncated": len(pending_ids) > LEDGER_EVENT_ID_CAP,
                "oldest_pending_age_seconds": round(self._oldest_pending_age(), 3),
            }
            with open(self.ledger_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._state["open_episode"] = episode_id
            self._persist_state()
        logger.warning("transport: outage %s opened (%s): %s", episode_id, kind, detail)
        return episode_id

    def _close_outage(self, delivered: int) -> None:
        with self._lock:
            episode_id = self._state.get("open_episode")
            if not episode_id:
                return
            entry = {
                "event": "close",
                "episode_id": episode_id,
                "ts": _utcnow_iso(),
                "delivered": delivered,
            }
            with open(self.ledger_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._state["open_episode"] = None
            self._persist_state()

    def flush(self, *, max_retries: int | None = None) -> dict:
        """Deliver pending journal records to the sink with bounded retries and
        exponential backoff. Never raises; returns a summary dict."""
        retries = self.max_retries if max_retries is None else max(1, int(max_retries))
        delivered_total = 0
        attempts_used = 0
        last_detail = ""
        attempt = 0
        while True:
            with self._lock:
                cursor = int(self._state["cursor_offset"])
                batch = [
                    rec
                    for rec in self._journal_records
                    if rec.start_offset >= cursor and rec.end_offset > cursor
                ][: self.batch_size]
            if not batch:
                break
            attempt += 1
            attempts_used = attempt
            result = self._attempt_deliver(batch, attempt)
            last_detail = result["detail"]
            if result["ok"] and result["accepted"] > 0:
                accepted = result["accepted"]
                with self._lock:
                    self._state["cursor_offset"] = batch[accepted - 1].end_offset
                    self._state["delivered_count"] = int(self._state["delivered_count"]) + accepted
                    self._state["last_success_at"] = _utcnow_iso()
                    self._persist_state()
                delivered_total += accepted
                self._close_outage(delivered_total)
                if accepted < len(batch):
                    # Partial accept: only the accepted prefix is durable at the
                    # sink; the remainder is requeued and left pending for the
                    # next flush (never dropped, never double-counted).
                    break
                attempt = 0  # a success resets the retry budget for the next batch
                continue
            if result["ok"] and result["accepted"] == 0:
                # ok-but-nothing-accepted: don't spin; treat as exhaustion.
                break
            if attempt >= retries:
                break
            if self._state.get("open_episode") is None:
                self._open_outage("sink_unavailable", last_detail)
            self._sleep(min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1))))
        pending = self._journaled - int(self._state["delivered_count"])
        return {
            "ok": pending == 0,
            "attempts": attempts_used,
            "delivered": delivered_total,
            "pending": pending,
            "detail": last_detail or ("drained" if pending == 0 else "exhausted retries"),
        }

    # ------------------------------------------------------------------
    # Status (machine-readable fail-closed surface)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            journaled = self._journaled
            delivered = int(self._state["delivered_count"])
            pending = max(0, journaled - delivered)
            return {
                "journaled": journaled,
                "delivered": delivered,
                "pending": pending,
                "oldest_pending_age_seconds": round(self._oldest_pending_age(), 3),
                "last_success_at": self._state.get("last_success_at"),
                "cursor_offset": int(self._state["cursor_offset"]),
                "transport_dir": str(self.dir),
                "torn_truncations": int(self._state.get("torn_truncations", 0)),
                "outage_open": self._state.get("open_episode") is not None,
                "sink_configured": self.sink is not None,
            }


# ---------------------------------------------------------------------------
# /telemetry-status plugin command
# ---------------------------------------------------------------------------


def handle_status(raw_args: str = "") -> str:
    """Slash-command handler: human-readable summary, or JSON with 'json' arg."""
    args = (raw_args or "").strip().lower()
    try:
        t = Transport()
        record = t.status()
    except Exception as exc:  # noqa: BLE001 — the status surface must never crash a session
        return f"telemetry-status unavailable: {exc}"
    if args in ("json", "--json", "-j"):
        return json.dumps(record, indent=2, sort_keys=True)
    lines = [
        "Telemetry transport (fail-closed local journal)",
        f"  journaled:       {record['journaled']}",
        f"  delivered:       {record['delivered']}",
        f"  pending:         {record['pending']}",
        f"  oldest pending:  {record['oldest_pending_age_seconds']}s",
        f"  last success:    {record['last_success_at'] or 'never'}",
        f"  outage open:     {record['outage_open']}",
        f"  torn truncations:{record['torn_truncations']}",
        f"  store:           {record['transport_dir']}",
    ]
    if record["pending"] > 0:
        lines.append("  NOTE: pending events are durably journaled — nothing was lost;")
        lines.append("  they will be delivered when the sink recovers (flush).")
    return "\n".join(lines)
