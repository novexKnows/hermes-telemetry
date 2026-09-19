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
   event); every later flush that still ends with pending records durably
   refreshes the open episode (``update`` entries carry the current pending
   set). Nothing is ever dropped silently: if the sink is down, events stay
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

``deliver`` returns ``{"ok": bool, "accepted": int, "detail": str}``. The
result is validated STRICTLY: all three keys must be present with exact types
(``ok: bool``, ``accepted: int`` (not bool, 0..len(batch)), ``detail: str``);
any non-conforming result (including a missing ``accepted``) is a failed
attempt and NEVER advances the cursor.
``accepted`` counts events durable at the sink IN ORDER — events beyond the
accepted prefix remain pending and are retried (partial accept). A duplicate
``event_id`` (crash between sink-accept and cursor-persist) counts as accepted;
the sink dedupes it. Timeout enforcement is the sink's responsibility (B4 owns
exporter HTTP timeouts); the transport treats any exception or non-conforming
result as a failed attempt.

Envelope precondition (ENFORCED, not assumed): the transport receives
ALREADY-REDACTED envelope dicts (B1 owns redaction; redaction always runs
before enqueue). ``enqueue`` enforces a concrete metadata-only boundary before
journal append: the envelope must conform to the pinned R-OBS-00 v1.0.0 schema
shape (see ``PINNED_SCHEMA_SHA256`` and ``_validate_envelope_shape``), must
contain no unknown fields, and must not carry secret-shaped strings or
prompt/response/file content sentinels in ANY field. The full Draft-2020-12
validation against the embedded pinned schema copy is MANDATORY and
FAIL-CLOSED: a missing/unreadable schema copy or an unusable ``jsonschema``
package rejects the event — it never silently degrades to the structural
checks alone. Violations raise ValueError before journal append — nothing
non-conforming is ever journaled or delivered.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

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

# -----------------------------------------------------------------------------
# Envelope precondition (metadata-only / schema boundary — R-OBS-00 v1.0.0)
#
# ENFORCED, not just documented: `enqueue` rejects (before journal append) any
# envelope that violates the pinned correlation-envelope v1.0.0 shape or carries
# banned content in ANY field. The pinned schema copy lives at
# C:/tmp/sovav-hermes/r-obs-00/docs/telemetry/correlation-envelope.schema.json;
# its sha256 at pin time is recorded below and checked by a drift test
# (tests/test_transport.py::test_pinned_schema_hash_matches).
# -----------------------------------------------------------------------------

#: sha256 of the pinned correlation-envelope.schema.json (v1.0.0).
PINNED_SCHEMA_SHA256 = "5271d5faa8bee8ad3d58885f4ab54f88d77af6e0620c78ba627665ca8433e70a"

#: Path of the pinned schema copy whose hash is recorded above. Used only by a
#: drift test (skipped when the copy is not present on the machine).
PINNED_SCHEMA_PATH = "C:/tmp/sovav-hermes/r-obs-00/docs/telemetry/correlation-envelope.schema.json"

#: Embedded pinned copy of correlation-envelope.schema.json (v1.0.0), shipped
#: with the plugin at docs/telemetry/ and byte-identical to PINNED_SCHEMA_PATH
#: (the drift test proves the byte equality via PINNED_SCHEMA_SHA256). This is
#: the copy enqueue validates against at runtime — enforcement must not depend
#: on a machine-specific path being present.
_EMBEDDED_SCHEMA_PATH = (
    Path(__file__).parent / "docs" / "telemetry" / "correlation-envelope.schema.json"
)

_SCHEMA_VALIDATOR_LOCK = threading.Lock()
_SCHEMA_VALIDATOR: object | None = None


def _pinned_schema() -> dict | None:
    """Load the embedded pinned schema copy; None when missing/unreadable.

    Callers MUST treat None as a fail-closed signal (see
    ``_jsonschema_violations``): the boundary never degrades to best-effort.
    """
    try:
        return json.loads(_EMBEDDED_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonschema_violations(envelope: dict) -> list[str]:
    """FULL pinned-schema enforcement: Draft-2020-12 validation against the
    embedded v1.0.0 copy (enums, types, minLength, allOf conditionals — the
    complete schema semantics, not just property/required-set parity).

    MANDATORY and FAIL-CLOSED: an unreadable/missing pinned copy or an
    unusable ``jsonschema`` package returns violations — the event is
    rejected, never silently accepted. The runtime dependency is declared in
    pyproject.toml and installed by CI, so the usable path is the normal one;
    anything that disables it must fail the enqueue, not bypass the pinned
    schema (regression: tests/test_transport.py::test_enqueue_fails_closed_*).
    """
    schema = _pinned_schema()
    if schema is None:
        return [
            "fail-closed: pinned schema copy unreadable or missing at "
            f"{_EMBEDDED_SCHEMA_PATH} — event rejected rather than bypassing "
            "the pinned v1.0.0 schema"
        ]
    global _SCHEMA_VALIDATOR
    with _SCHEMA_VALIDATOR_LOCK:
        if _SCHEMA_VALIDATOR is None:
            try:
                import jsonschema
            except ImportError:
                return [
                    "fail-closed: jsonschema package unavailable — event rejected "
                    "rather than bypassing full Draft-2020-12 validation (declare/"
                    "install the dependency; see pyproject.toml)"
                ]
            try:
                validator_cls = jsonschema.validators.validator_for(schema)
                validator_cls.check_schema(schema)
                _SCHEMA_VALIDATOR = validator_cls(schema)
            except Exception as exc:  # noqa: BLE001 — fail closed on any validator failure
                return [
                    f"fail-closed: pinned schema validator unusable ({type(exc).__name__}: {exc})"
                ]
        errors = sorted(_SCHEMA_VALIDATOR.iter_errors(envelope), key=str)
    return [f"schema violation: {err.message}" for err in errors]


#: Full v1.0.0 property set (schema: additionalProperties false).
ENVELOPE_SCHEMA_FIELDS = frozenset(
    {
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
        "repo",
        "agent_run_id",
        "correlation_id",
        "idempotency_key",
        "deal_id",
        "worker_id",
        "model_ref",
        "trigger_type",
        "runtime_environment",
        "basis_fingerprint",
        "worker_definition_version",
        "output_schema_version",
        "harness_task_id",
        "harness_run_id",
    }
)

#: Non-null string fields additionally REQUIRED on Turnstone-originated events.
_TURNSTONE_REQUIRED = (
    "agent_run_id",
    "correlation_id",
    "idempotency_key",
    "deal_id",
    "worker_id",
    "model_ref",
    "trigger_type",
    "runtime_environment",
    "basis_fingerprint",
    "worker_definition_version",
    "output_schema_version",
    "harness_task_id",
    "harness_run_id",
)

#: Extension fields that must be null on harness-originated (non-Turnstone)
#: events when present.
_NULLABLE_EXTENSIONS = _TURNSTONE_REQUIRED

#: Non-empty-string base fields of the pinned schema (handoff_id is str|None).
_STR_BASE_FIELDS = (
    "board",
    "task_id",
    "profile/role",
    "model",
    "provider",
    "key_id",
    "worktree/branch",
    "trace_id",
)

ENVELOPE_CALL_CLASSES = frozenset({"primary", "fallback", "aux", "delegate", "judge", "cron"})

#: Secret-shaped string patterns (credential materials must never appear in an
#: already-redacted metadata envelope). Matched against EVERY string value in
#: the envelope, at any depth.
_SECRET_SHAPED_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bsk-[A-Za-z0-9_-]{16,}",  # OpenAI-style API keys
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",  # GitHub tokens
        r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key ids
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",  # Slack tokens
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",  # PEM private key blocks
        r"\bBearer [A-Za-z0-9\-._~+/]{16,}",  # raw bearer credentials
    )
)

#: Explicit content sentinels: if one of these markers survives into any field,
#: the caller put prompt/response/file CONTENT into the envelope instead of
#: redacting it down to metadata — reject before journaling.
_CONTENT_SENTINEL_MARKERS = ("PROMPT_TEXT", "RESPONSE_TEXT", "FILE_CONTENT")

# ---------------------------------------------------------------------------
# Unicode default-ignorable normalization (mirrors the B1 redaction boundary:
# redaction.py `_strip_default_ignorables`). Default-ignorable code points are
# invisible in display but ARE part of the string value, so an attacker can
# interleave them into a sentinel marker or secret (e.g. "PROM<U+200B>PT_TEXT")
# to defeat raw shape matching. They are stripped BEFORE every sentinel and
# secret-pattern check below. Cover: the Cf / Zl / Zp general categories plus
# the default-ignorable outliers whose category is Lo / Mn / Cn.
# ---------------------------------------------------------------------------

_DEFAULT_IGNORABLE_CATEGORIES = frozenset({"Cf", "Zl", "Zp"})

_EXTRA_DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER (Mn)
    (0x115F, 0x1160),  # HANGUL JUNGSEONG/JONGSEONG FILLER (Lo)
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ/AA (Mn)
    (0x180B, 0x180D),  # MONGOLIAN FREE VARIATION SELECTOR ONE-THREE (Mn)
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR-1..16 (Mn)
    (0x2065, 0x2065),  # unassigned, default-ignorable (Cn)
    (0x3164, 0x3164),  # HANGUL FILLER (Lo)
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER (Lo)
    (0xFFF0, 0xFFF8),  # unassigned, default-ignorable (Cn)
    (0xE0000, 0xE0FFF),  # tag characters / variation selectors supplement (Cn)
)


def _is_default_ignorable(ch: str) -> bool:
    if unicodedata.category(ch) in _DEFAULT_IGNORABLE_CATEGORIES:
        return True
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EXTRA_DEFAULT_IGNORABLE_RANGES)


def _strip_default_ignorables(value: str) -> str:
    """Remove every Unicode default-ignorable character from ``value``."""
    if not value:
        return value
    return "".join(ch for ch in value if not _is_default_ignorable(ch))


def _iter_string_values(value: object) -> Iterator[tuple[str, str]]:
    """Yield (path, string) for every string reachable inside ``value``."""
    if isinstance(value, str):
        yield "", value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key, key
            yield from _iter_string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_string_values(item)


def _redaction_violations(envelope: dict) -> list[str]:
    """Banned-content scan: secret-shaped strings and content sentinels in any
    field of the (allegedly already-redacted) envelope.

    Every string (keys included) is canonicalized with
    ``_strip_default_ignorables`` BEFORE matching, so default-ignorable Unicode
    (e.g. a U+200B ZERO WIDTH SPACE inside "PROMPT_TEXT") cannot defeat the
    scan — same boundary as the B1 redaction layer."""
    violations: list[str] = []
    for path, s in _iter_string_values(envelope):
        # Strip default-ignorables FIRST; sentinels match case-insensitively,
        # secret patterns keep their case-sensitive semantics on the raw case.
        normalized = _strip_default_ignorables(s)
        lowered = normalized.lower()
        for marker in _CONTENT_SENTINEL_MARKERS:
            if marker.lower() in lowered:
                violations.append(f"content sentinel {marker!r} in field value at {path!r}")
        for pattern in _SECRET_SHAPED_PATTERNS:
            if pattern.search(normalized):
                violations.append(f"secret-shaped string (pattern {pattern.pattern!r}) at {path!r}")
    return violations


def _validate_envelope_shape(envelope: dict) -> None:
    """Enforce the pinned correlation-envelope v1.0.0 shape (metadata-only
    boundary) BEFORE journal append. Raises ValueError on any violation.

    This is the concrete enforcement of the already-redacted-envelope
    precondition: an envelope that does not conform to the pinned metadata
    schema, or that carries secret-shaped or content-sentinel strings, is
    rejected — it is never journaled, never delivered.
    """
    violations: list[str] = []
    unknown = sorted(set(envelope) - ENVELOPE_SCHEMA_FIELDS)
    if unknown:
        violations = (
            f"unknown field(s) {unknown} (schema v1.0.0 allows only the pinned property set)"
        )
        raise ValueError(f"envelope rejected before journal append: {violations}")
    for field in ENVELOPE_BASE_FIELDS:
        if field not in envelope:
            violations.append(f"missing required base field {field!r}")
    for field in _STR_BASE_FIELDS:
        val = envelope.get(field)
        if field in envelope and (not isinstance(val, str) or not val):
            violations.append(f"field {field!r} must be a non-empty string, got {val!r}")
    run_id = envelope.get("run_id")
    if "run_id" in envelope and (
        isinstance(run_id, bool)
        or not (isinstance(run_id, int) or (isinstance(run_id, str) and run_id))
    ):
        violations.append(f"field run_id must be int or non-empty str, got {run_id!r}")
    parent_ids = envelope.get("parent_task_ids")
    if "parent_task_ids" in envelope and (
        not isinstance(parent_ids, list)
        or not all(isinstance(x, str) for x in parent_ids)
        or len(set(parent_ids)) != len(parent_ids)
    ):
        violations.append(
            f"field parent_task_ids must be a list of unique strings, got {parent_ids!r}"
        )
    call_class = envelope.get("call_class")
    if "call_class" in envelope and (
        not isinstance(call_class, str) or call_class not in ENVELOPE_CALL_CLASSES
    ):
        violations.append(
            f"field call_class not in {sorted(ENVELOPE_CALL_CLASSES)}: {call_class!r}"
        )
    handoff_id = envelope.get("handoff_id")
    if (
        "handoff_id" in envelope
        and handoff_id is not None
        and (not isinstance(handoff_id, str) or not handoff_id)
    ):
        violations.append(f"field handoff_id must be str or null, got {handoff_id!r}")
    repo = envelope.get("repo")
    if "repo" in envelope and repo not in ("turnstone", None):
        violations.append(
            f'field repo must be "turnstone" or null (pinned v1.0.0 enum), got {repo!r}'
        )
    if repo == "turnstone":
        for req in _TURNSTONE_REQUIRED:
            val = envelope.get(req)
            if req == "runtime_environment":
                if val not in ("dev", "staging", "production"):
                    violations.append(
                        f"Turnstone event: runtime_environment must be "
                        f"dev/staging/production, got {val!r}"
                    )
            elif not isinstance(req_val := envelope.get(req), str) or not req_val:
                violations.append(f"Turnstone event: field {req!r} must be a non-empty string")
    else:
        for req in _NULLABLE_EXTENSIONS:
            if req in envelope and envelope[req] is not None:
                violations.append(
                    f"non-Turnstone event: extension field {req!r} must be null/absent"
                )
        ht, hr = envelope.get("harness_task_id"), envelope.get("harness_run_id")
        if (isinstance(ht, str) and ht) != (isinstance(hr, str) and hr):
            violations.append("harness_task_id and harness_run_id must be carried together")
    violations.extend(_redaction_violations(envelope))
    # Full pinned-schema enforcement (Draft-2020-12): catches every remaining
    # schema semantic the structural checks above don't already cover (enums,
    # types, minLength, the allOf Turnstone/harness conditionals).
    violations.extend(_jsonschema_violations(envelope))
    if violations:
        raise ValueError(
            "envelope rejected before journal append (metadata-only boundary): "
            + "; ".join(violations)
        )


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
        """Validate, then durably journal an already-redacted envelope; return
        the receipt.

        Enforces the metadata-only/schema boundary BEFORE journal append
        (R-OBS-00 v1.0.0 shape + banned-content scan): an envelope that fails
        the pinned schema shape, or carries secret-shaped / content-sentinel
        strings, raises ValueError and is never journaled.
        """
        if not isinstance(envelope, dict):
            raise TypeError("enqueue expects an envelope dict (redaction runs before enqueue)")
        _validate_envelope_shape(envelope)
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
        # STRICT exact validation of the pinned sink-result contract. A
        # non-conforming result is a failed attempt: the cursor is NEVER
        # advanced on this path (a missing `accepted` must never be silently
        # defaulted to a full-batch acceptance — that silently loses events).
        if not isinstance(result, dict):
            return {
                "ok": False,
                "accepted": 0,
                "detail": f"non-conforming sink result (not a dict): {result!r}",
            }
        missing = [key for key in ("ok", "accepted", "detail") if key not in result]
        if missing:
            return {
                "ok": False,
                "accepted": 0,
                "detail": f"non-conforming sink result: missing field(s) {missing}: {result!r}",
            }
        ok = result["ok"]
        if type(ok) is not bool:
            return {
                "ok": False,
                "accepted": 0,
                "detail": f"non-conforming sink result: ok must be bool, got {type(ok).__name__}",
            }
        accepted = result["accepted"]
        # type() not isinstance(): bool is a subclass of int and True/False are
        # not valid counts; accepted beyond len(batch) means the sink claims
        # durability it cannot have — reject rather than clamp silently.
        if type(accepted) is not int:
            return {
                "ok": False,
                "accepted": 0,
                "detail": f"invalid accepted count (must be int): {accepted!r}",
            }
        if accepted < 0 or accepted > len(batch):
            return {
                "ok": False,
                "accepted": 0,
                "detail": f"invalid accepted count (out of range 0..{len(batch)}): {accepted!r}",
            }
        detail = result["detail"]
        if not isinstance(detail, str):
            return {
                "ok": False,
                "accepted": 0,
                "detail": f"non-conforming sink result: detail must be str, got {detail!r}",
            }
        return {"ok": ok, "accepted": accepted, "detail": detail}

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

    def _pending_event_ids(self) -> list[str]:
        """Event ids of every record not yet accepted (cursor not advanced
        past it)."""
        with self._lock:
            cursor = int(self._state["cursor_offset"])
            return [rec.event_id for rec in self._journal_records if rec.start_offset >= cursor]

    def _append_ledger(self, entry: dict) -> None:
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _outage_pending_fields(self) -> dict:
        pending_ids = self._pending_event_ids()
        return {
            "pending_count": len(pending_ids),
            "pending_event_ids": pending_ids[:LEDGER_EVENT_ID_CAP],
            "pending_event_ids_truncated": len(pending_ids) > LEDGER_EVENT_ID_CAP,
            "oldest_pending_age_seconds": round(self._oldest_pending_age(), 3),
        }

    def _open_outage(self, kind: str, detail: str) -> str:
        episode_id = "ep_" + uuid.uuid4().hex[:12]
        entry = {
            "event": "open",
            "episode_id": episode_id,
            "ts": _utcnow_iso(),
            "kind": kind,
            "detail": detail,
        }
        entry.update(self._outage_pending_fields())
        with self._lock:
            self._append_ledger(entry)
            self._state["open_episode"] = episode_id
            self._persist_state()
        logger.warning("transport: outage %s opened (%s): %s", episode_id, kind, detail)
        return episode_id

    def _update_outage(self, kind: str, detail: str) -> None:
        """Durable refresh of an ALREADY-OPEN outage episode: every flush that
        ends with pending records while an episode is open appends an
        ``update`` entry with the CURRENT pending ids/count/staleness (the
        episode id is unchanged). Without this, work queued after the outage
        opened would never appear in the ledger."""
        with self._lock:
            episode_id = self._state.get("open_episode")
            if not episode_id:
                return
            entry = {
                "event": "update",
                "episode_id": episode_id,
                "ts": _utcnow_iso(),
                "kind": kind,
                "detail": detail,
            }
            entry.update(self._outage_pending_fields())
            self._append_ledger(entry)
        logger.warning("transport: outage %s updated (%s): %s", episode_id, kind, detail)

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
        last_ok = False
        partial = False
        attempt = 0
        episode_opened_this_flush = False
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
            last_ok = result["ok"]
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
                    partial = True
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
                episode_opened_this_flush = True
            self._sleep(min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1))))
        pending = self._journaled - int(self._state["delivered_count"])
        if pending > 0:
            # Fail-closed invariant (module docstring §3): EVERY flush that
            # ends with pending records must durably open OR UPDATE the outage
            # ledger — including one-attempt failure (max_retries=1),
            # ok-but-nothing-accepted, and partial accept, and including an
            # episode that is ALREADY open from an earlier flush (the open
            # entry must be refreshed with the current pending set, never left
            # stale). No silent outage.
            if partial:
                kind = "partial_accept_pending"
            elif last_ok:
                kind = "sink_accepted_none"
            else:
                kind = "sink_unavailable"
            detail = last_detail or "flush ended with pending records"
            if self._state.get("open_episode") is None:
                self._open_outage(kind, detail)
            elif not episode_opened_this_flush:
                # The episode was opened by an EARLIER flush; new pending work
                # exists, so the durable record must be refreshed.
                self._update_outage(kind, detail)
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
