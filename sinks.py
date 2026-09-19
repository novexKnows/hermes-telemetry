"""B4 sandbox sinks — LangSmith and Sentry exporters for R-OBS-00 envelopes.

Implements the PINNED SINK INTERFACE from B3's ``transport.py`` (not imported
here: B4 is deliberately independent of merge order — the interface shape, not
the symbol, is the contract)::

    class Sink:
        def deliver(self, events: list[dict]) -> dict:  # -> {"ok", "accepted", "detail"}

Each event passed to ``deliver`` is a transport journal record::

    {
        "event_id": str,      # transport-assigned, stable across retries
        "enqueued_at": str,   # ISO-8601 UTC timestamp of the original enqueue
        "attempt": int,       # 1-based delivery attempt counter
        "envelope": {...},    # the R-OBS-00 correlation envelope, already redacted
    }

METADATA-ONLY GATE (telemetry-contract.md §3.1/§3.3 — structural, not
conventional): an exporter reads ONLY the four pinned record keys. An event
carrying any other top-level key is a redaction violation: it is REJECTED
(not forwarded, not silently stripped), named in ``detail``, and the batch
result reports where the accepted in-order prefix ends, per the pinned
partial-accept semantics. Inside the envelope, the exporter forwards the
envelope values as-is — redaction already happened upstream (B1); the
exporter adds no fields and has no API to add prompt/response/file content.

FAIL-CLOSED PRE-SEND LEAKAGE GATE (R-OBS-00 defense in depth): before any
POST, every string in the ACTUAL outbound payload (envelope values included)
has its Unicode default-ignorable characters (Cf/Zl/Zp categories plus known
outliers — the same strip B1's redaction.py applies before ITS matching)
stripped, and the normalized form is matched against sentinel-marker and
secret-shape patterns. This defeats obfuscation like U+2063 interleaved into
a banned marker inside an allowed envelope value: the plain value would
survive verbatim forwarding and only normalization recovers the marker. On a
hit the event is REJECTED (no POST occurs, the in-order prefix stops) and
the violation names only the payload location — never the value. Clean
strings are delivered with ignorables already stripped (nothing invisible
survives to the vendor).

Dedupe: like B3's ``LocalFileSink``, a duplicate ``event_id`` counts as
accepted but is not re-transmitted. Server-side, LangSmith keeps exactly ONE
create per run id — a re-POST of an already-received run create returns
409 "Run create payload already received" (verified live) — so the
deterministic per-event run id makes retries idempotent: a 409 is counted as
accepted-duplicate, never a batch failure.

Credentials (telemetry-contract.md §3.4): read from the environment ONLY, by
env-var NAME — ``LANGSMITH_API_KEY`` / ``SENTRY_DSN``. No key values ever
appear in code, docs, logs or test fixtures.

LangSmith tenant scoping (verified live 2026-09-19): a valid ``lsv2_sk_``
key authenticates (``GET /api/v1/workspaces`` -> 200) but every tenant-scoped
endpoint (``POST /runs``, ``GET /api/v1/sessions``) returns 403 unless the
request carries ``X-Tenant-Id: <workspace id>`` — the server cannot resolve
the workspace from the key alone. The exporter therefore resolves a workspace
id (constructor arg > ``LANGSMITH_WORKSPACE_ID``/``LANGSMITH_TENANT_ID`` env >
runtime discovery via ``GET /api/v1/workspaces``) and sends it on every write.
The workspace id is tenant metadata returned to any authenticated caller, not
a secret. The official SDK read path supports the same header via its
``workspace_id`` parameter / ``LANGSMITH_WORKSPACE_ID`` env.

Timeouts are the sink's responsibility (B3 contract); ``timeout`` bounds every
HTTP call. Export failures never raise out of ``deliver`` — the pinned return
dict carries the failure so B3's transport can journal the outage.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Callable
from urllib import request as _urlrequest

__all__ = [
    "Sink",
    "PINNED_EVENT_KEYS",
    "LangSmithExporter",
    "SentryExporter",
    "langsmith_run_id_for_event",
    "parse_sentry_dsn",
]

# The exact top-level keys an exporter may read from a delivered record
# (pinned by B3's journal record shape — see module docstring).
PINNED_EVENT_KEYS = frozenset({"event_id", "enqueued_at", "attempt", "envelope"})


class Sink:
    """Pinned sink interface — B4 exporters implement ``deliver`` exactly."""

    def deliver(self, events: list[dict]) -> dict:
        """Deliver a batch; return ``{"ok": bool, "accepted": int, "detail": str}``."""
        raise NotImplementedError


def _result(ok: bool, accepted: int, detail: str) -> dict:
    return {"ok": bool(ok), "accepted": int(accepted), "detail": str(detail)}


def langsmith_run_id_for_event(event_id: str) -> str:
    """Deterministic LangSmith run id for a transport ``event_id``.

    Same event -> same run id on every retry, so duplicate deliveries are
    idempotent server-side, mirroring B3's dedupe-on-``event_id`` reference
    behaviour.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-telemetry:event:{event_id}"))


def _extract(event: dict) -> tuple[str | None, str | None, dict | None, str | None]:
    """Pull ``(event_id, enqueued_at, envelope, violation)`` from one record.

    ``violation`` is non-None when the record carries keys outside the pinned
    set (redaction precondition failure) or is missing required keys — the
    event is then unusable and must NOT be forwarded.
    """
    if not isinstance(event, dict):
        return None, None, None, "event is not a dict"
    extra = sorted(set(event) - set(PINNED_EVENT_KEYS))
    if extra:
        # Names only — never values (§3.3: a violation must fail loudly, not leak).
        return None, None, None, f"non-pinned keys present (redaction gate): {extra}"
    missing = [k for k in ("event_id", "enqueued_at", "envelope") if event.get(k) is None]
    if missing:
        return None, None, None, f"missing required keys: {missing}"
    envelope = event["envelope"]
    if not isinstance(envelope, dict):
        return None, None, None, "envelope is not a dict"
    return str(event["event_id"]), str(event["enqueued_at"]), envelope, None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fail-closed pre-send leakage gate. Mirrors B1 redaction.py's normalization
# (strip default-ignorables BEFORE matching) so the sink's last-line gate is
# never weaker than the emitter's scrubber: Unicode default-ignorable code
# points are invisible in display but ARE part of the string value, so an
# attacker can interleave them into banned material inside an allowed
# envelope value (e.g. U+2063 inside SENTINEL-SECRET-VALUE) to defeat plain
# substring matching. The strip runs over the ACTUAL outbound payload, the
# normalized form is matched, and a hit rejects the event with no POST.
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


# Sentinel-marker prefix (contract §3.3 redaction-leakage scan markers, e.g.
# SENTINEL-SECRET-VALUE) matched case-insensitively, plus the secret-shape
# patterns mirrored from B1 redaction.py (conservative: must not false-positive
# on ordinary model ids like "gpt-4o" or "z-ai/glm-5.3-flash").
_SENTINEL_MARK_RE = re.compile(r"SENTINEL-", re.IGNORECASE)

_SECRET_SHAPE_RES: tuple[re.Pattern, ...] = (
    # OpenAI-style keys: sk-...
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    # Anthropic-style keys
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    # GitHub tokens
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    # AWS
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Slack
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    # Bearer / token headers
    re.compile(r"(?:Bearer|bearer|Token|token)\s+[A-Za-z0-9._~+/-]{16,}={0,2}"),
    # Sentry DSNs (16+ hex chars as the public key — B1 redaction.py mirror)
    re.compile(r"https://[0-9a-f]{16,}@[a-z0-9.-]+/[0-9a-f]*", re.IGNORECASE),
    # Private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # URLs embedding credentials: scheme://user:secret@host
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE),
)


def _banned_material(normalized: str) -> bool:
    """True when the normalized string matches a sentinel marker or secret shape."""
    if _SENTINEL_MARK_RE.search(normalized):
        return True
    return any(pattern.search(normalized) for pattern in _SECRET_SHAPE_RES)


def _normalize_outbound(node: object, path: str) -> tuple[object, str | None]:
    """Normalize one outbound-payload node; return ``(normalized, violation)``.

    Every string (value or JSON key) has its default-ignorable characters
    stripped, and the normalized form is matched against sentinel/secret
    shapes. ``violation`` is non-None on a hit — the caller must reject the
    event without any POST and must never echo the offending value. Keys are
    normalized too: they ride in the outbound JSON exactly like values.

    Key locations in ``violation`` are opaque: because the raw (or even the
    normalized) key text is attacker-controlled, it is never interpolated
    into the detail path — positional ``key[i]`` labels are used instead
    (R4a2: an obfuscated banned key must not be echoed back in detail).
    """
    if isinstance(node, dict):
        normalized: dict = {}
        for index, (key, value) in enumerate(node.items()):
            # Opaque key location: report ``path.key[i]``, never the key text.
            child_path = f"{path}.key[{index}]"
            if isinstance(key, str):
                plain_key = _strip_default_ignorables(key)
                if _banned_material(plain_key):
                    return None, f"banned material at {child_path} (key; value withheld)"
            else:
                plain_key = key
            clean, violation = _normalize_outbound(value, child_path)
            if violation:
                return None, violation
            normalized[plain_key] = clean
        return normalized, None
    if isinstance(node, list):
        cleaned = []
        for index, item in enumerate(node):
            clean, violation = _normalize_outbound(item, f"{path}[{index}]")
            if violation:
                return None, violation
            cleaned.append(clean)
        return cleaned, None
    if isinstance(node, str):
        plain = _strip_default_ignorables(node)
        if _banned_material(plain):
            return None, f"banned material at {path} (value withheld)"
        return plain, None
    return node, None


def _default_post(url: str, body: dict, headers: dict, timeout: float) -> int:
    """One blocking HTTP POST of a JSON body. Returns the HTTP status code.

    Raises ``urllib.error.URLError``/``HTTPError``/``socket.timeout`` on
    failure — callers normalize to the pinned result dict.
    """
    data = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    req = _urlrequest.Request(
        url, data=data, headers=dict(headers, **{"Content-Type": "application/json"}), method="POST"
    )
    with _urlrequest.urlopen(req, timeout=timeout) as resp:
        return int(resp.status)


def _default_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    """One blocking HTTP GET. Returns ``(status, body_text)``.

    Raises like ``_default_post`` — callers normalize to the pinned result
    dict. Used only for tenant discovery; bodies are metadata, never secrets.
    """
    req = _urlrequest.Request(url, headers=dict(headers), method="GET")
    with _urlrequest.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read().decode("utf-8", "replace")


def _finish_batch(
    exporters_detail: str,
    events: list[dict],
    accepted: int,
    dupes: int,
    violations: list[str],
    failures: list[str],
) -> dict:
    """Shared batch summary for both exporters (pinned result dict)."""
    detail = f"{exporters_detail}: accepted {accepted}, deduped {dupes}"
    if violations:
        detail += f"; REJECTED at first violation: {violations[0]}"
    if failures:
        detail += f"; failed at first failure: {failures[0]}"
    return _result(not violations and not failures, accepted, detail)


# ---------------------------------------------------------------------------
# LangSmith
# ---------------------------------------------------------------------------


class LangSmithExporter(Sink):
    """Metadata-only R-OBS-00 envelope exporter to a LangSmith project.

    One journal record -> one finished LangSmith run (trace root, ``run_type``
    ``"tool"``). Contract §1.6 mapping: the envelope is attached VERBATIM as
    the run's ``extra.metadata`` — JSON keys with ``/`` (``profile/role``,
    ``worktree/branch``) are legal in LangSmith metadata. ``inputs``/``outputs``
    are empty dicts (metadata-only: no prompt, response or file content ever).

    Tenant scoping: the LangSmith API cannot resolve the workspace from the
    API key alone (writes 403 without it), so a workspace id is resolved
    once — constructor arg, then ``LANGSMITH_WORKSPACE_ID``/``LANGCHAIN_``
    ``WORKSPACE_ID``/``LANGSMITH_TENANT_ID``/``LANGCHAIN_TENANT_ID`` env, then
    runtime discovery via ``GET /api/v1/workspaces`` (first non-deleted
    workspace visible to the key). The id is tenant metadata, not a secret;
    failures never name values. With an injected ``post`` transport (unit
    tests) discovery is disabled unless a ``get`` transport is also injected.

    Read-back: ``langsmith_run_id_for_event`` gives the deterministic run id to
    feed ``langsmith.Client(workspace_id=...).list_runs(project_name=...,
    run_ids=[...])`` — the SDK read path used by the integration test and
    ``docs/telemetry/sandbox-flow.md``.
    """

    DEFAULT_API_URL = "https://api.smith.langchain.com"
    DEFAULT_PROJECT = "sovav-hermes-sandbox"  # TM-dispatched sandbox project
    RUN_NAME = "hermes.telemetry.event"
    RUN_TYPE = "tool"

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        project: str | None = None,
        workspace_id: str | None = None,
        timeout: float = 10.0,
        enabled: bool = True,
        post: Callable[[str, dict, dict, float], int] | None = None,
        get: Callable[[str, dict, float], tuple[int, str]] | None = None,
    ):
        self.api_key = (
            api_key
            or os.environ.get("LANGSMITH_API_KEY")
            or os.environ.get("LANGCHAIN_API_KEY")
            or ""
        )
        self.api_url = (
            api_url
            or os.environ.get("LANGSMITH_ENDPOINT")
            or os.environ.get("LANGCHAIN_ENDPOINT")
            or self.DEFAULT_API_URL
        ).rstrip("/")
        self.project = (
            project
            or os.environ.get("LANGSMITH_PROJECT")
            or os.environ.get("LANGCHAIN_PROJECT")
            or self.DEFAULT_PROJECT
        )
        raw_workspace = (
            workspace_id
            or os.environ.get("LANGSMITH_WORKSPACE_ID")
            or os.environ.get("LANGCHAIN_WORKSPACE_ID")
            or os.environ.get("LANGSMITH_TENANT_ID")
            or os.environ.get("LANGCHAIN_TENANT_ID")
            or ""
        ).strip()
        self.workspace_id: str | None = raw_workspace or None
        self.timeout = timeout
        self.enabled = enabled
        self._post = post or _default_post
        # Discovery transport: available in real mode (no injected post);
        # an injected post without an injected get means "no network beyond
        # the POSTs" — unit tests must never make discovery calls.
        self._get = get if get is not None else (_default_get if post is None else None)
        self._seen: set[str] = set()

    def resolve_workspace_id(self) -> None:
        """Discover the tenant workspace id (idempotent; sets ``workspace_id``).

        Picks the first non-deleted workspace the key can see (this key family
        is workspace-scoped; multi-workspace keys should be configured
        explicitly). Raises on failure — ``deliver`` normalizes to the pinned
        result dict without naming any value.
        """
        if self._get is None:
            return
        _status, body = self._get(
            f"{self.api_url}/api/v1/workspaces", {"x-api-key": self.api_key}, self.timeout
        )
        for ws in json.loads(body):
            if not ws.get("is_deleted"):
                self.workspace_id = str(ws["id"])
                return
        raise ValueError("no active workspace visible to this key")

    def make_run(self, event: dict) -> tuple[dict, str | None]:
        """Build the LangSmith run-create payload for one pinned record.

        Returns ``(payload, violation)``; ``violation`` is non-None when the
        event failed the pinned-keys gate OR the fail-closed pre-send leakage
        gate (banned material recovered from an allowed value after
        default-ignorable normalization) and must not be forwarded.
        """
        event_id, enqueued_at, envelope, violation = _extract(event)
        if violation:
            return {}, violation
        start = _parse_iso(enqueued_at)
        metadata = {**envelope, "event_id": event_id, "enqueued_at": enqueued_at}
        payload = {
            "id": langsmith_run_id_for_event(str(event_id)),
            "name": self.RUN_NAME,
            "run_type": self.RUN_TYPE,
            "session_name": self.project,
            "start_time": start.isoformat() if start else None,
            "end_time": start.isoformat() if start else None,
            "inputs": {},
            "outputs": {},
            "error": None,
            "extra": {"metadata": metadata},
        }
        # Pre-send gate over the ACTUAL outbound payload (docstring: fail-closed
        # leakage gate). Normalize first, match second; a hit rejects the event.
        payload, leak_violation = _normalize_outbound(payload, "payload")
        if leak_violation:
            return {}, leak_violation
        return payload, None

    def deliver(self, events: list[dict]) -> dict:
        """Pinned interface. ``accepted`` counts the in-order accepted prefix."""
        if not self.enabled:
            return _result(False, 0, "exporter disabled (kill switch)")
        if not self.api_key:
            return _result(False, 0, "no credentials: set LANGSMITH_API_KEY (env, never inline)")
        if self.workspace_id is None and self._get is not None:
            try:
                self.resolve_workspace_id()
            except Exception as exc:  # noqa: BLE001 — fail closed, names only
                return _result(
                    False,
                    0,
                    f"langsmith[{self.project}]: tenant resolution failed: {type(exc).__name__}",
                )
        headers = {"x-api-key": self.api_key}
        if self.workspace_id:
            # Tenant metadata (not a secret) — required or every write 403s.
            headers["X-Tenant-Id"] = self.workspace_id
        accepted = 0
        dupes = 0
        violations: list[str] = []
        failures: list[str] = []
        for event in events:
            payload, violation = self.make_run(event)
            if violation:
                # Redaction gate or malformed record: STOP here so nothing
                # beyond it is forwarded; report the violation verbatim.
                violations.append(violation)
                break
            event_id = str(event["event_id"])
            if event_id in self._seen:
                dupes += 1  # counts as accepted (B3 reference semantics)
                accepted += 1
                continue
            try:
                self._post(f"{self.api_url}/runs", payload, headers, self.timeout)
            except Exception as exc:  # noqa: BLE001 — normalized to the pinned dict
                if getattr(exc, "code", None) == 409:
                    # "Run create payload already received" — the server keeps
                    # exactly one create per run id, so a retried event (same
                    # deterministic id) is already durably ingested. Accepted,
                    # not re-ingested; never a batch failure.
                    self._seen.add(event_id)
                    dupes += 1
                    accepted += 1
                    continue
                failures.append(f"{event_id}: {type(exc).__name__}")
                break
            self._seen.add(event_id)
            accepted += 1
        return _finish_batch(
            f"langsmith[{self.project}]", events, accepted, dupes, violations, failures
        )


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------


def _sentry_tag_key(name: str) -> str:
    """Contract §1.6: tag keys allow only [A-Za-z0-9_.:-]; '/' -> '_'."""
    return name.replace("/", "_")


def parse_sentry_dsn(dsn: str) -> dict | None:
    """Parse a Sentry DSN into its components (the public key stays in memory)."""
    if not dsn:
        return None
    try:
        scheme, rest = dsn.split("://", 1)
        key, rest = rest.split("@", 1)
        host, project_id = rest.rsplit("/", 1)
        if not (scheme and key and host and project_id.isdigit()):
            return None
        return {"scheme": scheme, "public_key": key, "host": host, "project_id": project_id}
    except (ValueError, AttributeError):
        return None


class SentryExporter(Sink):
    """Metadata-only envelope exporter to a Sentry project's store endpoint.

    One journal record -> one Sentry event with the envelope attached as TAGS
    (contract §1.6: ``/`` in tag keys normalized to ``_``; non-string values
    serialized as strings) plus an ``extra`` metadata block carrying the
    envelope verbatim. Credential: ``SENTRY_DSN`` env var (§3.4) — the store
    endpoint derives from the DSN itself, never a separate host config.

    VERIFICATION STATUS (HTB-B4): the LangSmith leg is verified end-to-end
    against the live sandbox; the Sentry leg is credential-gated (no
    SENTRY_DSN provisioned), so this exporter is mock-tested only and its
    integration test skips without a DSN. ``sentry_verified`` is NOT claimed.
    """

    DEFAULT_ENV_DSN = "SENTRY_DSN"
    RUN_MESSAGE = "R-OBS-00 metadata-only telemetry event"

    def __init__(
        self,
        dsn: str | None = None,
        environment: str = "dev",
        timeout: float = 10.0,
        enabled: bool = True,
        post: Callable[[str, dict, dict, float], int] | None = None,
    ):
        self.dsn = dsn or os.environ.get(self.DEFAULT_ENV_DSN) or ""
        self.environment = environment
        self.timeout = timeout
        self.enabled = enabled
        self._post = post or _default_post
        self._seen: set[str] = set()
        self._dsn_parts = parse_sentry_dsn(self.dsn)

    @property
    def configured(self) -> bool:
        return self._dsn_parts is not None

    def make_event(self, event: dict, timestamp: str) -> tuple[dict, str | None]:
        """Build a minimal Sentry store payload for one pinned record."""
        event_id, enqueued_at, envelope, violation = _extract(event)
        if violation:
            return {}, violation
        tags: dict[str, str] = {"environment": self.environment}
        for key, value in envelope.items():
            tags[_sentry_tag_key(key)] = value if isinstance(value, str) else json.dumps(value)
        tags["event_id"] = str(event_id)
        payload = {
            "event_id": langsmith_run_id_for_event(str(event_id)).replace("-", ""),
            "timestamp": timestamp,
            "platform": "python",
            "environment": self.environment,
            "level": "info",
            "logger": "hermes.telemetry",
            "message": self.RUN_MESSAGE,
            "tags": tags,
            "extra": {"envelope": envelope, "enqueued_at": enqueued_at},
        }
        return payload, None

    def _store_url(self) -> str:
        parts = self._dsn_parts or {}
        return f"{parts.get('scheme', 'https')}://{parts.get('host', '')}/api/{parts.get('project_id', '')}/store/"

    def _store_headers(self) -> dict:
        parts = self._dsn_parts or {}
        # DSN-derived store-protocol auth; the public key never appears in
        # payloads, logs or detail strings.
        return {
            "X-Sentry-Auth": f"Sentry sentry_key={parts.get('public_key', '')}, sentry_version=7"
        }

    def deliver(self, events: list[dict]) -> dict:
        if not self.enabled:
            return _result(False, 0, "exporter disabled (kill switch)")
        if not self._dsn_parts:
            return _result(
                False, 0, f"no credentials: set {self.DEFAULT_ENV_DSN} (env, never inline)"
            )
        accepted = 0
        dupes = 0
        violations: list[str] = []
        failures: list[str] = []
        for event in events:
            payload, violation = self.make_event(event, timestamp=_utc_now_iso())
            if violation:
                violations.append(violation)
                break
            event_id = str(event["event_id"])
            if event_id in self._seen:
                dupes += 1
                accepted += 1
                continue
            try:
                self._post(self._store_url(), payload, self._store_headers(), self.timeout)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{event_id}: {type(exc).__name__}")
                break
            self._seen.add(event_id)
            accepted += 1
        return _finish_batch(
            f"sentry[{self._dsn_parts.get('project_id', '?')}]",
            events,
            accepted,
            dupes,
            violations,
            failures,
        )
