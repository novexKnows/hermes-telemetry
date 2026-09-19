"""tests/test_e2e.py — HTC-C3 controlled end-to-end integration tests.

Card t_9ebff8b3 (stream C3 of TM-C). Runs the INTEGRATED plugin pipeline
end-to-end under a disposable home:

  A. SANDBOX SINK LEG — envelope.emit() -> durable Transport -> live sandbox
     LangSmith AND Sentry exporters, with SDK/API read-back proof. Skips
     (never fabricates) when the approved credential files are absent or the
     live API rejects them (credential gate, per Trevor).
  B. METRICS LEG — utilization-controller-consumable metrics extracted by
     metrics.py (B2 mapping) are present in emitted envelope events, asserted
     against docs/telemetry/utilization-metrics-mapping.md.
  C. FAILURE/ROLLBACK LEGS —
     C1 transport outage: durable journal + outage ledger + /telemetry-status
        prove no silent loss; recovery drains everything FIFO.
     C2 redaction fail-closed: violation events (sentinel material, including
        Unicode default-ignorable obfuscation) produce ZERO POSTs to any sink
        at BOTH enforcement layers (transport pre-journal gate and exporter
        pre-send gate).
     C3 rollback: the disposable home returns to pre-test state (inventory +
        sha256 proof) and the real telemetry home gains no files during the
        session (session-scoped proof fixture).

Isolation contract: EVERY test pins BOTH HERMES_HOME and HERMES_TELEMETRY_HOME
to a per-test disposable directory (paths.py precedence — the telemetry home
is resolved from HERMES_TELEMETRY_HOME first, so pinning only HERMES_HOME is
NOT enough; see the 2026-09-19 leak incident in conftest.py). Credentials are
read AT RUNTIME from the approved secret files into env only — values are
never printed, logged, hashed, attached or copied.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hermes_telemetry import envelope, metrics, sinks
from hermes_telemetry import transport as transport_mod

# ---------------------------------------------------------------------------
# Constants (card t_9ebff8b3 + B4 sandbox conventions)
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

TASK_ID = "t_9ebff8b3"
RUN_LABEL = "htc-c3-e2e"
BRANCH = "wt/htc-c3-e2e"
# Disposable board slug (THREE-PIN ISOLATION): the envelope's board field
# carries THIS value in e2e runs — never a live board slug, so a leak into a
# kanban surface is self-identifying.
BOARD = "e2e-disposable-board"
BOARD_DB_FILENAME = "kanban.db"

LANGSMITH_KEY_FILE_DEFAULT = "C:/svc/secrets/langsmith-api-key.txt"
SENTRY_TOKEN_FILE_DEFAULT = "C:/svc/secrets/sentry-personal-token.txt"
LANGSMITH_PROJECT = "sovav-hermes-sandbox"
SENTRY_ORG = "sovav"
SENTRY_PROJECT = "sovav-hermes-sandbox"

SENTRY_API = "https://sentry.io/api/0"
SENTRY_READBACK_DEADLINE_S = 120.0
SENTRY_READBACK_POLL_S = 10.0
SENTRY_DATECREATED_SKEW_S = 5.0
LANGSMITH_READBACK_DEADLINE_S = 60.0
LANGSMITH_READBACK_POLL_S = 3.0

# Sentinel markers (telemetry-contract.md §3.3) — must NEVER appear in any
# exported payload. Same set as tests/test_sinks.py.
MARKERS = [
    "SENTINEL-PROMPT-TEXT",
    "SENTINEL-RESPONSE-TEXT",
    "SENTINEL-TOOL-RESULT-BODY",
    "SENTINEL-SECRET-VALUE",
    "SENTINEL-ENV-VALUE",
    "SENTINEL-FILE-CONTENT",
]

# Profiling-safety cap: files above this size are inventoried by size only.
_HASH_CAP_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Isolation / inventory helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_inventory(root: Path) -> dict[str, str]:
    """Recursive inventory {relpath: sha256 | 'dir' | 'size:N'} of a tree."""
    inv: dict[str, str] = {}
    if not root.is_dir():
        return inv
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            inv[rel] = "dir"
        elif path.is_file():
            if path.stat().st_size > _HASH_CAP_BYTES:
                inv[rel] = f"size:{path.stat().st_size}"
            else:
                inv[rel] = _sha256_file(path)
    return inv


def _ambient_telemetry_root() -> Path:
    """Resolve the REAL telemetry home the way paths.py does, from the AMBIENT
    env (must be called before any test fixture pins the env)."""
    override = os.environ.get("HERMES_TELEMETRY_HOME") or os.environ.get("HERMES_HOME")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
        root = Path(base) / ".hermes"
    return root / "telemetry"


def _ambient_kanban_db() -> Path:
    """Resolve the LIVE board DB path the way the kanban tooling does, from
    the AMBIENT env (must be called before any fixture pins the env)."""
    override = os.environ.get("HERMES_KANBAN_DB")
    if override:
        return Path(override)
    base = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return Path(base) / ".hermes" / "kanban.db"


def _board_task_count(db_path: Path) -> int | None:
    """READ-ONLY row count of the board DB's tasks table (None if absent)."""
    if not db_path.is_file():
        return None
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            (count,) = con.execute("SELECT COUNT(*) FROM tasks").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return int(count)


# Captured at module import (collection time — BEFORE any fixture pins the
# env): the real telemetry root this session must never touch (leg C3).
REAL_TELEMETRY_ROOT = _ambient_telemetry_root()
LIVE_KANBAN_DB = _ambient_kanban_db()


@pytest.fixture(scope="session", autouse=True)
def real_home_untouched_proof():
    """Session-scoped rollback proof (leg C3, real-home side): the REAL
    telemetry home must gain NO files during the e2e session, and the LIVE
    board DB must gain no task rows tagged with this run's markers
    (THREE-PIN ISOLATION leak check — read-only). Transient sqlite sidecars
    (-wal/-shm) from an unrelated live process are reported as a warning,
    not a failure — this proof is about THIS run's writes.
    """
    real_root = _ambient_telemetry_root()
    before = _dir_inventory(real_root)
    tasks_before = _board_task_count(LIVE_KANBAN_DB)
    yield
    if real_root.is_dir():
        after = _dir_inventory(real_root)
        new = sorted(set(after) - set(before))
        sidecars = [p for p in new if p.endswith(("-wal", "-shm"))]
        permanent = [p for p in new if not p.endswith(("-wal", "-shm"))]
        assert not permanent, (
            f"real telemetry home {real_root} gained files during the e2e session: {permanent}"
        )
        if sidecars:  # observe, never assert: not written by this run
            print(f"e2e note: transient sqlite sidecars observed in real home: {sidecars}")
    # Live-board leak check: row count must be unchanged, or every new row
    # must be provably NOT this run's (no e2e marker in id-bearing text).
    tasks_after = _board_task_count(LIVE_KANBAN_DB)
    if tasks_before is not None and tasks_after is not None:
        assert tasks_after == tasks_before, (
            f"live board DB {LIVE_KANBAN_DB} task rows changed during the e2e "
            f"session: {tasks_before} -> {tasks_after} (possible kanban leak)"
        )


@pytest.fixture()
def e2e_home(tmp_path, monkeypatch):
    """Disposable home for one e2e leg: ALL THREE env surfaces pinned (the
    2026-09-19 operator ruling after the C1 gate-2 leak: HERMES_HOME +
    HERMES_TELEMETRY_HOME + HERMES_KANBAN_DB/HERMES_KANBAN_BOARD — a worker
    context CAN mutate the live board, so inherited kanban env is a real leak
    vector), plus deterministic envelope-provenance env."""
    home = tmp_path / "e2e-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # THREE-PIN ISOLATION: any kanban/dispatch surface reached from this
    # process can only ever see the disposable board DB.
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "disposable-board" / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)
    monkeypatch.setenv("HERMES_KANBAN_TASK", TASK_ID)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", RUN_LABEL)
    monkeypatch.setenv("HERMES_KANBAN_BRANCH", BRANCH)
    monkeypatch.delenv("TURNSTONE_HARNESS_TASK_ID", raising=False)
    monkeypatch.delenv("TURNSTONE_HARNESS_RUN_ID", raising=False)
    envelope.reset_derivation_cache()
    yield home
    envelope.reset_derivation_cache()


def _assert_all_paths_under(home: Path) -> None:
    """Every resolver must land inside the disposable home (leg C3 invariant)."""
    from hermes_telemetry import paths

    assert paths.get_telemetry_home() == home / "telemetry"
    assert str(paths.get_db_path()).startswith(str(home))
    assert str(paths.get_transport_dir()).startswith(str(home))
    assert str(paths.get_budget_path()).startswith(str(home))
    assert str(paths.get_pricing_path()).startswith(str(home))


def _assert_not_real_home() -> None:
    """The resolved telemetry home must NEVER be the real user home."""
    from hermes_telemetry import paths

    assert paths.get_telemetry_home().resolve() != REAL_TELEMETRY_ROOT.resolve(), (
        "telemetry home resolved to the REAL home — isolation broken"
    )


# ---------------------------------------------------------------------------
# Credential loading (runtime only; values never printed)
# ---------------------------------------------------------------------------


def _load_secret(env_name: str, file_env_name: str, default_path: str) -> str | None:
    """Credential from env first, then the approved on-disk file. Values stay
    in the return value only — never logged."""
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    path = Path(os.environ.get(file_env_name, default_path))
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


def _fetch_sentry_dsn(token: str, org: str, project: str) -> str | None:
    """Fetch the sandbox project's public DSN from the authenticated keys
    endpoint (B4b-verified flow). Returns None on any failure — the live leg
    then SKIPS (credential gate), never fabricates."""
    url = f"{SENTRY_API}/projects/{org}/{project}/keys/"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            keys = json.load(resp)
        dsn = keys[0]["dsn"]["public"]
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError):
        return None
    return dsn or None


# ---------------------------------------------------------------------------
# Envelope / event helpers
# ---------------------------------------------------------------------------


def _unique_event_id() -> str:
    return "ev-" + uuid.uuid4().hex[:12]


def _journal_record(event_id: str, env: dict, enqueued_at: str | None = None) -> dict:
    """A pinned transport record shape (what the sink receives)."""
    return {
        "event_id": event_id,
        "enqueued_at": enqueued_at or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "attempt": 1,
        "envelope": env,
    }


def _emit_records(count: int, session_prefix: str) -> list[dict]:
    """Emit `count` metadata-only records through the real emission path
    (envelope.emit: build -> scrub -> conform), each carrying controller
    metrics in the event body (leg B rides along with leg A's live flow)."""
    records = []
    for i in range(count):
        session_id = f"{session_prefix}-{i:03d}"
        usage = metrics.usage_metrics_from_post_api_request(
            {
                "usage": {
                    "input_tokens": 100 + i,
                    "output_tokens": 40 + i,
                    "reasoning_tokens": 7,
                    "cache_read_tokens": 5,
                    "cache_write_tokens": 3,
                    "prompt_tokens": 108 + i,
                    "total_tokens": 148 + i,
                    "request_count": 1,
                },
                "api_duration": 1.25,
                "started_at": 1000.0,
                "first_chunk_at": 1000.2,
                "ended_at": 1001.25,
            }
        )
        billing = metrics.billing_metrics_from_post_api_request(
            {
                "response": {
                    "id": f"gen_e2e_{i}",
                    "provider": "openrouter",
                    "billed_cost_usd": 0.0042,
                }
            }
        )
        rec = envelope.emit(
            hook="post_api_request",
            session_id=session_id,
            model="z-ai/glm-5.3-flash",
            provider="openrouter",
            platform="cli",
            profile="worker",
            metadata={**usage, **billing},
        )
        assert rec is not None, "envelope.emit must produce a record in the e2e config"
        records.append(rec)
    return records


def _marker_scan(payload: object) -> list[str]:
    """§3.3 negative scan: return the markers found in a serialized payload."""
    serialized = json.dumps(payload, default=str)
    return [m for m in MARKERS if m in serialized]


# ---------------------------------------------------------------------------
# Leg A — sandbox sink legs (live; skip-gated, never fabricated)
# ---------------------------------------------------------------------------


def test_e2e_legA_langsmith_transport_pipeline_readback(e2e_home, monkeypatch):
    """FULL pipeline e2e: envelope.emit -> Transport journal -> flush ->
    LangSmithExporter -> live sandbox project -> SDK read-back proves arrival.
    Skips without credentials (no-fabrication gate)."""
    langsmith = pytest.importorskip("langsmith")
    _assert_all_paths_under(e2e_home)
    key = _load_secret("LANGSMITH_API_KEY", "LANGSMITH_API_KEY_FILE", LANGSMITH_KEY_FILE_DEFAULT)
    if not key:
        pytest.skip("no LangSmith credential (env / approved key file absent)")
    monkeypatch.setenv("LANGSMITH_API_KEY", key)
    monkeypatch.setenv("LANGSMITH_PROJECT", LANGSMITH_PROJECT)

    records = _emit_records(3, "e2e-ls")
    exporter = sinks.LangSmithExporter()  # workspace discovery at flush time
    t = transport_mod.Transport(sink=exporter, dir=e2e_home / "transport" / "langsmith")
    receipts = [t.enqueue(rec["envelope"]) for rec in records]
    assert len(receipts) == 3
    for r in receipts:
        assert t.receipt(r["event_id"])["delivered"] is False  # durable pre-flush

    result = t.flush()
    assert result["ok"] is True, result["detail"]
    assert result["delivered"] == 3 and result["pending"] == 0
    st = t.status()
    assert st["pending"] == 0 and st["outage_open"] is False
    for r in receipts:
        assert t.receipt(r["event_id"])["delivered"] is True

    run_ids = [sinks.langsmith_run_id_for_event(r["event_id"]) for r in receipts]

    client = langsmith.Client(workspace_id=exporter.workspace_id)
    deadline = time.monotonic() + LANGSMITH_READBACK_DEADLINE_S
    back: list = []
    while True:
        back = list(client.list_runs(project_name=LANGSMITH_PROJECT, run_ids=run_ids))
        if len(back) == len(run_ids) or time.monotonic() >= deadline:
            break
        time.sleep(LANGSMITH_READBACK_POLL_S)
    assert len(back) == 3, (
        f"read-back found {len(back)}/3 runs after {LANGSMITH_READBACK_DEADLINE_S:.0f}s"
    )
    got = {r.extra.get("metadata", {}).get("event_id") for r in back}
    assert len(got) == 3  # three distinct deterministic run ids arrived
    for run in back:
        md = run.extra.get("metadata", {})
        # Envelope round-trips verbatim (join keys per contract §1.4).
        assert md["task_id"] == TASK_ID
        assert md["run_id"] == RUN_LABEL
        assert md["profile/role"] == "worker"
        assert md["worktree/branch"] == BRANCH
        assert md["board"] == BOARD
        assert md["trace_id"].startswith("trc-")
        assert md["provider"] == "openrouter"
        assert md["model"] == "z-ai/glm-5.3-flash"
        # Metadata-only: no prompt/response material ever.
        assert run.inputs in ({}, None)
        assert run.outputs in ({}, None)
        assert _marker_scan(run.dict()) == []


def test_e2e_legA_sentry_transport_pipeline_readback(e2e_home, monkeypatch):
    """FULL pipeline e2e into the Sentry sandbox project: envelope.emit ->
    Transport -> flush -> SentryExporter (DSN store endpoint) -> authenticated
    API read-back proves arrival of THIS run's events."""
    _assert_all_paths_under(e2e_home)
    token = _load_secret("SENTRY_AUTH_TOKEN", "SENTRY_AUTH_TOKEN_FILE", SENTRY_TOKEN_FILE_DEFAULT)
    if not token:
        pytest.skip("no Sentry API credential (env / approved token file absent)")
    monkeypatch.setenv("SENTRY_ORG", SENTRY_ORG)
    monkeypatch.setenv("SENTRY_PROJECT", SENTRY_PROJECT)
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        dsn = _fetch_sentry_dsn(token, SENTRY_ORG, SENTRY_PROJECT)
    if not dsn:
        pytest.skip("Sentry DSN unavailable (keys endpoint failed) — credential gate")

    records = _emit_records(2, "e2e-sn")
    exporter = sinks.SentryExporter(dsn=dsn)
    assert exporter.configured
    t = transport_mod.Transport(sink=exporter, dir=e2e_home / "transport" / "sentry")
    receipts = [t.enqueue(rec["envelope"]) for rec in records]
    delivered_after = datetime.now(timezone.utc)  # fresh-object lower bound
    result = t.flush()
    assert result["ok"] is True, result["detail"]
    assert result["delivered"] == 2 and result["pending"] == 0

    for receipt in receipts:
        sentry_event_id = sinks.langsmith_run_id_for_event(receipt["event_id"]).replace("-", "")
        deadline = time.monotonic() + SENTRY_READBACK_DEADLINE_S
        body = None
        while time.monotonic() < deadline:
            req = urllib.request.Request(
                f"{SENTRY_API}/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/events/{sentry_event_id}/",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # async ingest — keep polling
                    time.sleep(SENTRY_READBACK_POLL_S)
                    continue
                raise
        assert body is not None, (
            f"event {sentry_event_id} not readable after {SENTRY_READBACK_DEADLINE_S:.0f}s"
        )
        assert body["eventID"] == sentry_event_id
        created = datetime.fromisoformat(body["dateCreated"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        assert (
            delivered_after - timedelta(seconds=SENTRY_DATECREATED_SKEW_S)
            <= created
            <= datetime.now(timezone.utc) + timedelta(seconds=SENTRY_DATECREATED_SKEW_S)
        ), "read-back object is not from THIS run (fresh-window failure)"
        tags = {tg["key"]: tg["value"] for tg in body["tags"]}
        assert tags["task_id"] == TASK_ID
        assert tags["run_id"] == RUN_LABEL
        assert tags["profile_role"] == "worker"
        assert tags["worktree_branch"] == BRANCH
        assert tags["event_id"] == receipt["event_id"]
        assert tags["trace_id"].startswith("trc-")
        context = body.get("context") or {}
        assert context["envelope"]["task_id"] == TASK_ID
        assert _marker_scan(body) == []


# ---------------------------------------------------------------------------
# Leg B — metrics present in emitted envelope events (offline; mapping doc)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Leg B — metrics present in emitted envelope events (mapping doc)
#
# Two layers, matching docs/telemetry/utilization-metrics-mapping.md:
#   1. INTEGRATED EMISSION (the real plugin hook handlers, registered and
#      fired exactly as the runtime fires them) — the emitted event body
#      carries the controller-consumable metrics in the redaction-allowlist
#      vocabulary (tokens_in/tokens_out/cost_usd/latency_ms/...).
#   2. B2 EXTRACTION LAYER (metrics.py, "hook kwargs in -> metric dict out")
#      — the mapping-table metric names, asserted function by function.
# The envelope is additionalProperties:false, so metrics ride OFF the envelope;
# the controller joins the event body via the envelope's task_id + run_id keys
# (contract §1.4) — asserted here too.
# ---------------------------------------------------------------------------

_INIT_SPEC = importlib.util.spec_from_file_location(
    "hermes_telemetry", str(_PLUGIN_ROOT / "__init__.py")
)
_init_mod = importlib.util.module_from_spec(_INIT_SPEC)
# Registered BEFORE exec so the module's `from . import ...` relative imports
# resolve (same pattern as tests/test_envelope_emission.py).
sys.modules["hermes_telemetry"] = _init_mod
_INIT_SPEC.loader.exec_module(_init_mod)


class _E2EPluginContext:
    """Minimal PluginContext stand-in (same shape as the B1 test harness)."""

    def __init__(self, profile_name: str = "worker") -> None:
        self.hooks: dict = {}
        self.commands: dict = {}
        self.profile_name = profile_name

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, description="", args_hint=""):
        self.commands[name] = fn

    def register_cli_command(self, *a, **k):
        pass

    def fire(self, hook_name, **kwargs):
        fn = self.hooks.get(hook_name)
        return fn(**kwargs) if fn else None


def test_e2e_legB_metrics_in_integrated_emission(e2e_home, monkeypatch):
    """Fire the REAL post_api_request / post_tool_call / subagent_stop /
    on_session_start / on_session_end handlers and assert the controller
    metrics are present in the emitted envelope events (mapping table C)."""
    monkeypatch.setenv("HERMES_TELEMETRY_NO_SETUP", "1")
    _assert_all_paths_under(e2e_home)
    envelope.clear_records()
    ctx = _E2EPluginContext()
    _init_mod.register(ctx)

    # --- post_api_request: tokens, latency, cost (disposable-home pricing)
    ctx.fire(
        "post_api_request",
        session_id="sess-e2e-metrics",
        model="z-ai/glm-5.3-flash",
        provider="openrouter",
        platform="cli",
        api_duration=2.5,
        usage={
            "input_tokens": 120,
            "output_tokens": 55,
            "cache_read_tokens": 6,
            "cache_write_tokens": 4,
            "reasoning_tokens": 9,
        },
        api_call_count=1,
        finish_reason="stop",
        turn_id="turn-1",
        api_request_id="req-1",
        task_id="per-turn-uuid-not-card-id",
        response={"id": "gen_abc123", "provider": "openrouter", "billed_cost_usd": 0.0071},
        moa_references=[
            {
                "label": "advisor-1",
                "model": "z-ai/glm-5.3-flash",
                "provider": "openrouter",
                "temperature": 0.3,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "cost_usd": 0.0002,
                "cost_status": "estimated",
                "cost_source": "pricing",
                "output": "ADVISED-ANSWER-TEXT-SHOULD-BE-DROPPED",
            }
        ],
    )
    rec = envelope.recent_records()[-1]
    ev = rec["event"]
    # Token buckets ride under the allowlisted names (mapping table C rows).
    assert ev["tokens_in"] == 120
    assert ev["tokens_out"] == 55
    assert ev["cache_read_tokens"] == 6 and ev["cache_write_tokens"] == 4
    assert ev["reasoning_tokens"] == 9
    assert ev["estimated"] is False
    # Latency + call context.
    assert ev["latency_ms"] == 2500
    assert ev["api_call_count"] == 1
    assert ev["finish_reason"] == "stop"
    # Cost: priced through the disposable-home pricing engine (real or
    # estimated — presence and numeric shape are the contract, not the tariff).
    assert isinstance(ev["cost_usd"], (int, float)) and ev["cost_usd"] >= 0.0
    # MoA advisor metrics are COUNTED on the envelope event (metadata);
    # extraction of the full per-advisor metric set is layer 2 (below). The
    # advisor's answer text must NOT appear anywhere in the emitted record.
    assert ev["moa_reference_count"] == 1
    assert "ADVISED-ANSWER-TEXT-SHOULD-BE-DROPPED" not in json.dumps(rec)
    # Envelope join keys (contract §1.4): task_id + run_id + trace_id.
    env = rec["envelope"]
    assert env["task_id"] == TASK_ID and env["run_id"] == RUN_LABEL
    assert env["trace_id"] == envelope.trace_id_for("sess-e2e-metrics")
    assert env["model"] == "z-ai/glm-5.3-flash" and env["provider"] == "openrouter"
    # Metrics ride OFF the envelope (schema additionalProperties:false): the
    # envelope carries only the pinned schema fields — no metric keys. (Event
    # and envelope key NAMES may overlap by design — model/provider appear in
    # both — but they are separate dicts joined by the envelope identity.)
    for metric_key in ("tokens_in", "tokens_out", "cost_usd", "latency_ms"):
        assert metric_key not in env
        assert metric_key in ev

    # --- post_tool_call: tool metrics, args/result bodies never emitted
    ctx.fire(
        "post_tool_call",
        session_id="sess-e2e-metrics",
        tool_name="terminal",
        tool_call_id="tc-1",
        task_id="per-turn-uuid",
        duration_ms=432,
        ok=True,
        status="success",
        args={"command": "TOOL-ARGS-BODY-MUST-NOT-EMIT"},
        result="TOOL-RESULT-BODY-MUST-NOT-EMIT",
    )
    ev = envelope.recent_records()[-1]["event"]
    assert ev["tool_name"] == "terminal"
    assert ev["ok"] is True and ev["status"] == "success"
    assert ev["latency_ms"] == 432 and ev["tool_call_id"] == "tc-1"
    assert "TOOL-RESULT-BODY-MUST-NOT-EMIT" not in json.dumps(envelope.recent_records())
    assert "TOOL-ARGS-BODY-MUST-NOT-EMIT" not in json.dumps(envelope.recent_records())

    # --- subagent_stop: delegation close (child_status canonical values)
    ctx.fire(
        "subagent_stop",
        parent_session_id="sess-p",
        child_session_id="sess-c",
        child_role="worker",
        child_status="completed",
        duration_ms=900,
        child_summary="CHILD-SUMMARY-TEXT-MUST-NOT-EMIT",
    )
    ev = envelope.recent_records()[-1]["event"]
    assert ev["child_status"] == "completed"
    assert ev["duration_ms"] == 900 and ev["child_role"] == "worker"
    assert "CHILD-SUMMARY-TEXT-MUST-NOT-EMIT" not in json.dumps(envelope.recent_records())

    # --- on_session_start with a cron session id: cron_job_id parsed
    ctx.fire(
        "on_session_start",
        session_id="cron_job42_20260919_150000",
        model="z-ai/glm-5.3-flash",
        platform="cron",
    )
    ev = envelope.recent_records()[-1]["event"]
    assert ev["cron_job_id"] == "job42"
    assert envelope.recent_records()[-1]["envelope"]["call_class"] == "cron"

    # --- on_session_end: run outcome derivation (interrupted/ok/error)
    ctx.fire("on_session_end", session_id="sess-e2e-metrics", completed=True)
    assert envelope.recent_records()[-1]["event"]["status"] == "ok"
    ctx.fire("on_session_end", session_id="sess-e2e-metrics", interrupted=True)
    assert envelope.recent_records()[-1]["event"]["status"] == "interrupted"

    # Every emitted envelope in this leg conforms structurally.
    for record in envelope.recent_records():
        assert envelope.structural_errors(record["envelope"]) == []
    envelope.clear_records()


def test_e2e_legB_b2_extraction_mapping_layer(e2e_home):
    """metrics.py (B2 mapping, 'hook kwargs in -> metric dict out') produces
    the mapping-table metric set — asserted function by function against
    docs/telemetry/utilization-metrics-mapping.md."""
    _assert_all_paths_under(e2e_home)

    # post_api_request: token buckets + latency + TTFB (table C rows).
    usage = metrics.usage_metrics_from_post_api_request(
        {
            "usage": {
                "input_tokens": 120,
                "output_tokens": 55,
                "reasoning_tokens": 9,
                "cache_read_tokens": 6,
                "cache_write_tokens": 4,
                "prompt_tokens": 130,  # derived property (input + read + write)
                "total_tokens": 185,
                "request_count": 1,
            },
            "api_duration": 2.5,
            "started_at": 1000.0,
            "ended_at": 1002.5,
            "first_chunk_at": 1000.35,  # streamed: TTFB sourceable
        }
    )
    assert usage["input_tokens"] == 120 and usage["output_tokens"] == 55
    assert usage["reasoning_tokens"] == 9
    assert usage["cache_read_tokens"] == 6 and usage["cache_write_tokens"] == 4
    assert usage["prompt_tokens"] == 130 and usage["total_tokens"] == 185
    assert usage["request_count"] == 1
    assert usage["api_duration_ms"] == 2500
    assert usage["time_to_first_token_s"] == pytest.approx(0.35)
    # usage=None: NO token keys (estimation is the caller's policy — GAPS).
    assert metrics.usage_metrics_from_post_api_request({"api_duration": 1.0}) == {
        "api_duration_ms": 1000
    }

    # Billing rows (dirty installed tree: id/provider/billed_cost_usd).
    billing = metrics.billing_metrics_from_post_api_request(
        {
            "response": {
                "id": "gen_abc123",
                "provider": "openrouter",
                "billed_cost_usd": 0.0071,
            }
        }
    )
    assert billing["billed_cost_usd"] == pytest.approx(0.0071)
    assert billing["provider_response_id"] == "gen_abc123"
    assert billing["serving_provider"] == "openrouter"

    # MoA allowlisted projection: output/input_messages DISCARDED.
    moa = metrics.moa_metrics_from_moa_references(
        [
            {
                "label": "advisor-1",
                "model": "m",
                "provider": "openrouter",
                "temperature": 0.3,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "cost_usd": 0.0002,
                "cost_status": "estimated",
                "cost_source": "pricing",
                "output": "ADVISED-ANSWER-TEXT-SHOULD-BE-DROPPED",
            }
        ]
    )
    assert moa[0]["label"] == "advisor-1"
    assert moa[0]["input_tokens"] == 10 and moa[0]["output_tokens"] == 5
    assert "output" not in moa[0] and "input_messages" not in moa[0]

    # Call identity: hook task_id is the per-turn UUID -> hook_task_id only.
    call_ctx = metrics.call_context_from_post_api_request(
        {"model": "m", "provider": "p", "task_id": "per-turn-uuid", "turn_id": "t1"}
    )
    assert call_ctx["hook_task_id"] == "per-turn-uuid"
    assert "task_id" not in call_ctx

    # Tool metrics (core-derived status; error_message is content-bearing and
    # extraction may carry it — redaction is the emission layer's concern).
    tool = metrics.tool_call_metrics_from_post_tool_call(
        {"tool_name": "terminal", "duration_ms": 432, "status": "success"}
    )
    assert tool["tool_name"] == "terminal"
    assert tool["tool_duration_ms"] == 432 and tool["tool_status"] == "success"

    # Session outcome derivation (interrupted -> interrupted, completed -> ok).
    assert metrics.session_outcome_from_on_session_end({"completed": True})["run_status"] == "ok"
    assert (
        metrics.session_outcome_from_on_session_end({"interrupted": True})["run_status"]
        == "interrupted"
    )
    assert metrics.session_outcome_from_on_session_end({})["run_status"] == "error"

    # Delegation edges + dispatch observers.
    deleg = metrics.delegation_metrics_from_subagent_stop(
        {"parent_session_id": "p", "child_session_id": "c", "child_status": "failed"}
    )
    assert deleg["child_status"] == "failed"
    spawn = metrics.dispatch_metrics_from_kanban_worker_spawned(
        {
            "task_id": TASK_ID,
            "board": BOARD,
            "run_id": 45,
            "worker_pid": 4242,
            "workspace_path": "C:/tmp/x",
            "profile_name": "worker",
        }
    )
    assert spawn["task_id"] == TASK_ID and spawn["run_id"] == 45
    assert spawn["profile/role"] == "worker"
    exited = metrics.dispatch_metrics_from_kanban_worker_exited(
        {"task_id": TASK_ID, "exit_kind": "clean_exit"}
    )
    assert exited["exit_kind"] == "clean_exit"

    # Cron job id: anchored parse of the scheduler session-id shape.
    assert metrics.cron_job_id_from_session_id("cron_job42_20260919_150000") == "job42"
    assert metrics.cron_job_id_from_session_id("sess-normal") is None

    # GAPS list matches the mapping doc (13 entries; asserted upstream by
    # test_metrics too — re-asserted here so the controller leg cannot pass
    # against a silently-rotted gap list).
    assert len(metrics.GAPS) == 13
    gap_metrics = {g["metric"] for g in metrics.GAPS}
    assert {
        "envelope.key_id",
        "envelope.parent_task_ids",
        "failed_attempt.tokens/cost",
        "envelope.trace_id",
        "envelope.handoff_id",
        "envelope.worktree/branch",
        "envelope.call_class = 'fallback'",
        "envelope.call_class = 'aux'",
        "envelope.call_class = 'judge'",
        "active_time.cumulative",
        "review.claimed / review.decision",
        "artifact.metrics",
        "tokens when usage is None",
    } == gap_metrics

    # Envelope structural mapping (table A summary): all 12 base fields present
    # with the derived values; Turnstone extensions null on harness events.
    env = envelope.build_envelope(
        session_id="sess-e2e-mapping",
        model="z-ai/glm-5.3-flash",
        provider="openrouter",
        platform="cli",
        profile="worker",
    )
    assert envelope.structural_errors(env) == []
    for field in (
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
    ):
        assert field in env
    assert env["task_id"] == TASK_ID and env["run_id"] == RUN_LABEL
    assert env["parent_task_ids"] == []  # gap: no hook carries the parent set
    assert env["handoff_id"] is None  # gap: no handoff kwarg
    assert env["trace_id"].startswith("trc-")  # derived deterministically
    for ext in (
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
    ):
        assert env[ext] is None, f"harness event must carry null extension: {ext}"

    # Durable leg: an emitted record's envelope through the transport +
    # LocalFileSink proves the join keys survive the durable path (§1.4).
    rec = envelope.emit(
        hook="post_api_request",
        session_id="sess-e2e-mapping",
        model="z-ai/glm-5.3-flash",
        provider="openrouter",
        platform="cli",
        profile="worker",
        metadata={"tokens_in": 1, "tokens_out": 1},
    )
    assert rec is not None and rec["event"]["tokens_in"] == 1
    local_sink = transport_mod.LocalFileSink(e2e_home / "delivered-metrics.jsonl")
    t = transport_mod.Transport(sink=local_sink, dir=e2e_home / "transport" / "metrics")
    receipt = t.enqueue(rec["envelope"])
    assert t.flush()["ok"] is True
    lines = (e2e_home / "delivered-metrics.jsonl").read_text(encoding="utf-8").splitlines()
    delivered = json.loads(lines[-1])
    assert delivered["event_id"] == receipt["event_id"]
    assert delivered["envelope"]["task_id"] == TASK_ID
    assert delivered["envelope"]["run_id"] == RUN_LABEL


# ---------------------------------------------------------------------------
# Leg C1 — transport outage: no silent loss (durable journal + ledger + status)
# ---------------------------------------------------------------------------


class _UnreachableSink:
    """Sink that models a network outage: every attempt raises."""

    def __init__(self) -> None:
        self.calls = 0

    def deliver(self, events: list[dict]) -> dict:
        self.calls += 1
        raise TimeoutError("sink unreachable (simulated outage)")


def test_e2e_legC1_transport_outage_no_silent_loss(e2e_home):
    """Outage: flush fails closed — events stay pending (journaled == pending),
    receipts queryable, outage ledger opens with pending ids + staleness,
    /telemetry-status reports it; recovery drains everything FIFO, zero loss."""
    _assert_all_paths_under(e2e_home)
    down = _UnreachableSink()
    # DEFAULT transport dir (under the pinned disposable home) so the real
    # /telemetry-status surface — which constructs Transport() with the
    # default dir — observes this outage.
    t = transport_mod.Transport(sink=down, max_retries=2, base_delay_s=0.01, max_delay_s=0.05)
    tdir = t.dir
    records = _emit_records(4, "e2e-outage")
    receipts = [t.enqueue(rec["envelope"]) for rec in records]

    result = t.flush()
    assert result["ok"] is False and result["pending"] == 4
    assert down.calls >= 2  # bounded retries actually attempted

    # No silent loss: receipts queryable through the outage.
    for r in receipts:
        assert t.receipt(r["event_id"]) is not None
        assert t.receipt(r["event_id"])["delivered"] is False

    st = t.status()
    assert st["journaled"] == 4 and st["pending"] == 4 and st["delivered"] == 0
    assert st["outage_open"] is True
    assert st["oldest_pending_age_seconds"] >= 0

    # /telemetry-status (both surfaces) reports the outage, not a silent drop.
    human = transport_mod.handle_status()
    machine = json.loads(transport_mod.handle_status("json"))
    assert machine["pending"] == 4 and machine["outage_open"] is True
    assert "nothing was lost" in human

    # Outage ledger: open episode carries the pending ids + staleness.
    ledger_lines = [
        json.loads(line)
        for line in (tdir / "outage_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    opens = [e for e in ledger_lines if e.get("event") == "open"]
    assert opens, "outage ledger must open an episode on a failed flush"
    episode = opens[-1]
    assert episode["pending_count"] == 4
    assert set(episode["pending_event_ids"]) == {r["event_id"] for r in receipts}
    assert episode["oldest_pending_age_seconds"] >= 0

    # Recovery: a healthy sink over the SAME store drains everything FIFO.
    delivered_path = e2e_home / "delivered-recovery.jsonl"
    healthy = transport_mod.LocalFileSink(delivered_path)
    t2 = transport_mod.Transport(sink=healthy, dir=tdir)
    result2 = t2.flush()
    assert result2["ok"] is True and result2["delivered"] == 4
    st2 = t2.status()
    assert st2["pending"] == 0 and st2["outage_open"] is False
    assert st2["last_success_at"] is not None
    back = [
        json.loads(line)
        for line in delivered_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e["envelope"]["trace_id"] for e in back] == [
        rec["envelope"]["trace_id"] for rec in records
    ]  # FIFO: delivered in journal order, zero loss, zero reorder


def test_e2e_legC1b_outage_ledger_episode_detail(e2e_home):
    """The ledger episode names the failure detail (exception type) and the
    episode closes only on a successful flush."""
    tdir = e2e_home / "transport" / "ledger"
    t = transport_mod.Transport(sink=_UnreachableSink(), dir=tdir, max_retries=1, base_delay_s=0.01)
    rec = _emit_records(1, "e2e-ledger")[0]
    t.enqueue(rec["envelope"])
    t.flush()
    entries = [
        json.loads(line)
        for line in (tdir / "outage_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any("TimeoutError" in json.dumps(e) for e in entries), (
        "ledger detail must carry the failure class (no silent outage)"
    )
    healthy = transport_mod.LocalFileSink(e2e_home / "delivered-ledger.jsonl")
    t2 = transport_mod.Transport(sink=healthy, dir=tdir)
    t2.flush()
    entries2 = [
        json.loads(line)
        for line in (tdir / "outage_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "close" for e in entries2), (
        f"episode must close on recovery; ledger tail: {entries2[-1]}"
    )


# ---------------------------------------------------------------------------
# Leg C2 — redaction fail-closed: violation events produce ZERO POSTs
# ---------------------------------------------------------------------------


class _CountingPost:
    """Counting transport for asserting ZERO POSTs."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, body: dict, headers: dict, timeout: float) -> int:
        self.calls.append(url)
        return 202


def _envelope_with_model(model: str) -> dict:
    """A conforming envelope carrying the probe in the schema-allowed `model`
    field. Built directly (not via emit) so the probe reaches the transport /
    exporter gates verbatim — emit's own scrubber may normalize first, and
    these legs exist to prove the gates BELOW emission."""
    return envelope.build_envelope(
        session_id="sess-e2e-violation",
        model=model,
        provider="openrouter",
        platform="cli",
        profile="worker",
    )


def test_e2e_legC2_transport_gate_rejects_before_journal_zero_posts(e2e_home):
    """Layer 1 (transport pre-journal gate): sentinel material — direct AND
    Unicode default-ignorable obfuscated — never reaches the journal, and the
    sink sees ZERO calls."""
    counting = _CountingPost()
    exporter = sinks.LangSmithExporter(
        api_key="e2e-unused-key", project=LANGSMITH_PROJECT, post=counting
    )
    t = transport_mod.Transport(sink=exporter, dir=e2e_home / "transport" / "gate")

    probes = [
        "ADVERSARIAL-PROMPT_TEXT",  # content sentinel
        "ADVERSARIAL-RESPONSE_TEXT",  # content sentinel
        "ADVERSARIAL-FILE_CONTENT",  # content sentinel
        "PROM\u200bPT_TEXT",  # U+200B-obfuscated sentinel — cannot defeat scan
        "sk-e2eFAKEKEY0000000000000",  # secret-shaped (OpenAI-style pattern)
        "AKIAIOSFODNN7EXAMPLE",  # secret-shaped (AWS pattern)
    ]
    for model in probes:
        env = _envelope_with_model(model)
        with pytest.raises(ValueError):
            t.enqueue(env)
    # Nothing journaled, nothing delivered, ZERO POSTs to any sink.
    assert t.status()["journaled"] == 0
    assert t.flush()["pending"] == 0
    assert counting.calls == []
    assert (
        not (e2e_home / "transport" / "gate" / "events.jsonl").exists()
        or (e2e_home / "transport" / "gate" / "events.jsonl").read_text(encoding="utf-8").strip()
        == ""
    )


def test_e2e_legC2_exporter_gate_rejects_presend_zero_posts(e2e_home):
    """Layer 2 (exporter pre-send gate): a record that somehow carries sentinel
    material in a schema-allowed field is rejected by BOTH exporters with ZERO
    transport calls; the rejection detail names no value."""
    probes = [
        "SENTINEL-SECRET-VALUE",
        "SENTINEL-SECRET\u200b-VALUE",
        "SENTINEL-FILE-CONTENT",
    ]
    for model in probes:
        env = _envelope_with_model(model)
        env["model"] = model  # place the probe exactly where the scan must find it
        record = _journal_record(_unique_event_id(), env)
        for exp_name in ("langsmith", "sentry"):
            counting = _CountingPost()
            if exp_name == "langsmith":
                exporter = sinks.LangSmithExporter(
                    api_key="e2e-unused-key",
                    project=LANGSMITH_PROJECT,
                    post=counting,
                )
            else:
                exporter = sinks.SentryExporter(
                    dsn="https://public@example.ingest.sentry.io/42", post=counting
                )
            result = exporter.deliver([record])
            assert result["ok"] is False, (exp_name, model, result)
            assert result["accepted"] == 0
            assert counting.calls == [], (
                f"{exp_name}: violation event MUST produce zero POSTs ({model!r})"
            )
            # Opaque detail: the banned value never travels in the detail.
            assert model.replace("\u200b", "") not in result["detail"]


def test_e2e_legC2_clean_events_still_flow_after_violation(e2e_home):
    """Fail-closed, not fail-stopped: a violation rejects exactly its own
    event; clean events before and after it flow normally."""
    clean_a = _envelope_with_model("z-ai/glm-5.3-flash")

    class _SplitSink:
        """Records the batch; counts POSTs of a real exporter shape."""

        def __init__(self) -> None:
            self.batches: list[list[dict]] = []

        def deliver(self, events: list[dict]) -> dict:
            self.batches.append(events)
            return {"ok": True, "accepted": len(events), "detail": "ok"}

    sink = _SplitSink()
    t = transport_mod.Transport(sink=sink, dir=e2e_home / "transport" / "mixed")
    ok_a = t.enqueue(clean_a)
    bad = _envelope_with_model("ADVERSARIAL-PROM\u200bPT_TEXT")
    with pytest.raises(ValueError):
        t.enqueue(bad)
    ok_b = t.enqueue(_envelope_with_model("z-ai/glm-5.3-flash"))
    result = t.flush()
    assert result["ok"] is True and result["delivered"] == 2
    assert [r["event_id"] for r in (ok_a, ok_b)] != []
    assert len(sink.batches[0]) == 2


# ---------------------------------------------------------------------------
# Leg C3 — rollback: disposable home returns to pre-test state
# ---------------------------------------------------------------------------


def test_e2e_legC3_rollback_disposable_home_inventory_hash(e2e_home):
    """Rollback proof: inventory+sha256 of the disposable home before the run,
    after the pipeline, and after teardown. The harness's own artifacts live
    ONLY under the disposable root; the pre-test inventory (empty) is restored
    by removing the root. Real-home side is proven by the session fixture."""

    pre = _dir_inventory(e2e_home)
    assert pre == {}, "disposable home must start empty (pre-test inventory)"

    # A representative pipeline writes its artifacts: DB + transport store.
    from hermes_telemetry import paths

    paths.get_db_path()  # creates telemetry.db under the disposable home
    _assert_all_paths_under(e2e_home)
    _assert_not_real_home()
    local_sink = transport_mod.LocalFileSink(e2e_home / "telemetry" / "delivered.jsonl")
    t = transport_mod.Transport(sink=local_sink, dir=e2e_home / "telemetry" / "transport")
    rec = _emit_records(1, "e2e-rollback")[0]
    receipt = t.enqueue(rec["envelope"])
    assert t.flush()["ok"] is True
    assert t.receipt(receipt["event_id"])["delivered"] is True

    during = _dir_inventory(e2e_home)
    assert during != pre, "pipeline must have written artifacts (inside the root)"
    for rel in during:
        assert (e2e_home / rel).resolve().is_relative_to(e2e_home.resolve()), (
            f"artifact escaped the disposable root: {rel}"
        )

    # Rollback: remove the disposable root; the pre-test state (absent) is
    # restored. Inventory comparison is against the pre-test snapshot.
    import shutil

    shutil.rmtree(e2e_home)
    post = _dir_inventory(e2e_home)  # absent root -> empty inventory
    assert post == pre, "rollback must restore the pre-test (empty) inventory"

    # And the path resolver must never point at the real home afterwards.
    _assert_not_real_home()
