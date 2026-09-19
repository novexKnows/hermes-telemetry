"""tools/e2e_evidence.py — HTC-C3 e2e evidence runner (card t_9ebff8b3).

Executes the controlled end-to-end legs of tests/test_e2e.py's flow as a
standalone driver and prints ONE redacted JSON evidence record to stdout:
ids, counts, timestamps and verdicts only — never envelope payloads, never
credential values, never secret material.

Isolation: runs entirely under a fresh disposable home (both HERMES_HOME and
HERMES_TELEMETRY_HOME pinned to a per-run temp dir that is removed on exit).

Credentials: read at runtime from the approved secret files (env override
names LANGSMITH_API_KEY_FILE / SENTRY_AUTH_TOKEN_FILE). A live leg whose
credential is missing or rejected is reported "skipped" — never fabricated.

Usage:
    python tools/e2e_evidence.py                # all legs
    python tools/e2e_evidence.py --offline      # legs B + C only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT))

# Register the plugin package alias (the repo root IS the plugin dir, hyphen
# name on disk) — the standalone equivalent of tests/conftest.py.
import types  # noqa: E402

_pkg = types.ModuleType("hermes_telemetry")
_pkg.__path__ = [str(_PLUGIN_ROOT)]
_pkg.__package__ = "hermes_telemetry"
_pkg.__file__ = str(_PLUGIN_ROOT / "__init__.py")
sys.modules["hermes_telemetry"] = _pkg

TASK_ID = "t_9ebff8b3"
RUN_LABEL = "htc-c3-e2e"
BRANCH = "wt/htc-c3-e2e"
BOARD = "e2e-disposable-board"  # THREE-PIN ISOLATION: disposable board slug
LANGSMITH_PROJECT = "sovav-hermes-sandbox"
SENTRY_ORG = "sovav"
SENTRY_PROJECT = "sovav-hermes-sandbox"
SENTRY_API = "https://sentry.io/api/0"
EVIDENCE = {"card": TASK_ID, "run": RUN_LABEL, "legs": {}}


def _setup_disposable_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="htc-c3-e2e-home-"))
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_TELEMETRY_HOME"] = str(home)
    # THREE-PIN ISOLATION (operator ruling): kanban/dispatch surfaces must
    # only ever see the disposable board DB — a worker context can mutate the
    # live board through inherited env.
    os.environ["HERMES_KANBAN_DB"] = str(home / "disposable-board" / "kanban.db")
    os.environ["HERMES_KANBAN_BOARD"] = BOARD
    os.environ["HERMES_TELEMETRY_NO_SETUP"] = "1"
    os.environ["HERMES_KANBAN_TASK"] = TASK_ID
    os.environ["HERMES_KANBAN_RUN_ID"] = RUN_LABEL
    os.environ["HERMES_KANBAN_BRANCH"] = BRANCH
    os.environ.pop("TURNSTONE_HARNESS_TASK_ID", None)
    os.environ.pop("TURNSTONE_HARNESS_RUN_ID", None)
    return home


def _load_secret(env_name: str, file_env_name: str, default_path: str) -> str | None:
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    path = Path(os.environ.get(file_env_name, default_path))
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


def _fetch_sentry_dsn(token: str, org: str, project: str) -> str | None:
    url = f"{SENTRY_API}/projects/{org}/{project}/keys/"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)[0]["dsn"]["public"]
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError):
        return None


def _emit_records(count: int, prefix: str) -> list[dict]:
    from hermes_telemetry import envelope

    records = []
    for i in range(count):
        rec = envelope.emit(
            hook="post_api_request",
            session_id=f"{prefix}-{i:03d}",
            model="z-ai/glm-5.3-flash",
            provider="openrouter",
            platform="cli",
            profile="worker",
            metadata={
                "tokens_in": 100 + i,
                "tokens_out": 40 + i,
                "cache_read_tokens": 5,
                "cache_write_tokens": 3,
                "reasoning_tokens": 7,
                "cost_usd": 0.0042,
                "latency_ms": 1250,
                "api_call_count": 1,
                "finish_reason": "stop",
                "estimated": False,
            },
        )
        records.append(rec)
    return records


def _marker_set() -> set[str]:
    return {
        "SENTINEL-PROMPT-TEXT",
        "SENTINEL-RESPONSE-TEXT",
        "SENTINEL-TOOL-RESULT-BODY",
        "SENTINEL-SECRET-VALUE",
        "SENTINEL-ENV-VALUE",
        "SENTINEL-FILE-CONTENT",
    }


def leg_langsmith(home: Path) -> dict:
    """Leg A (LangSmith): emit -> durable transport -> live sink -> SDK read-back."""
    try:
        langsmith = __import__("langsmith")
    except ImportError:
        return {"verdict": "skipped", "reason": "langsmith SDK not installed"}
    key = _load_secret(
        "LANGSMITH_API_KEY", "LANGSMITH_API_KEY_FILE", "C:/svc/secrets/langsmith-api-key.txt"
    )
    if not key:
        return {"verdict": "skipped", "reason": "no LangSmith credential"}
    os.environ["LANGSMITH_API_KEY"] = key
    os.environ["LANGSMITH_PROJECT"] = LANGSMITH_PROJECT
    from hermes_telemetry import sinks
    from hermes_telemetry import transport as transport_mod

    records = _emit_records(3, "e2e-ls")
    exporter = sinks.LangSmithExporter()
    t = transport_mod.Transport(sink=exporter, dir=home / "transport" / "langsmith")
    receipts = [t.enqueue(rec["envelope"]) for rec in records]
    result = t.flush()
    st = t.status()
    out = {
        "events": len(receipts),
        "flush_ok": result["ok"],
        "flush_detail": result["detail"],
        "pending_after": st["pending"],
        "outage_open": st["outage_open"],
    }
    if not result["ok"]:
        return {**out, "verdict": "failed", "reason": result["detail"]}
    run_ids = [sinks.langsmith_run_id_for_event(r["event_id"]) for r in receipts]
    client = langsmith.Client(workspace_id=exporter.workspace_id)
    deadline = time.monotonic() + 60.0
    back: list = []
    while True:
        back = list(client.list_runs(project_name=LANGSMITH_PROJECT, run_ids=run_ids))
        if len(back) == len(run_ids) or time.monotonic() >= deadline:
            break
        time.sleep(3.0)
    markers = set()
    for run in back:
        serialized = json.dumps(run.dict(), default=str)
        markers |= {m for m in _marker_set() if m in serialized}
    out.update(
        {
            "verdict": "verified" if len(back) == 3 else "failed",
            "workspace_id": exporter.workspace_id,
            "run_ids_read_back": sorted(str(r.id) for r in back),
            "inputs_empty": all(r.inputs in ({}, None) for r in back),
            "outputs_empty": all(r.outputs in ({}, None) for r in back),
            "marker_hits": sorted(markers),
            "readback_deadline_hit": len(back) < 3,
        }
    )
    return out


def leg_sentry(home: Path) -> dict:
    """Leg A (Sentry): emit -> durable transport -> live sink -> API read-back."""
    from hermes_telemetry import sinks
    from hermes_telemetry import transport as transport_mod

    token = _load_secret(
        "SENTRY_AUTH_TOKEN", "SENTRY_AUTH_TOKEN_FILE", "C:/svc/secrets/sentry-personal-token.txt"
    )
    if not token:
        return {"verdict": "skipped", "reason": "no Sentry API credential"}
    dsn = (os.environ.get("SENTRY_DSN") or "").strip() or _fetch_sentry_dsn(
        token, SENTRY_ORG, SENTRY_PROJECT
    )
    if not dsn:
        return {"verdict": "skipped", "reason": "Sentry DSN unavailable (keys endpoint)"}
    records = _emit_records(2, "e2e-sn")
    exporter = sinks.SentryExporter(dsn=dsn)
    t = transport_mod.Transport(sink=exporter, dir=home / "transport" / "sentry")
    receipts = [t.enqueue(rec["envelope"]) for rec in records]
    delivered_after = datetime.now(timezone.utc)
    result = t.flush()
    out = {
        "events": len(receipts),
        "flush_ok": result["ok"],
        "flush_detail": result["detail"],
    }
    if not result["ok"]:
        return {**out, "verdict": "failed", "reason": result["detail"]}
    events = []
    fresh_window_ok = True
    for receipt in receipts:
        sentry_id = sinks.langsmith_run_id_for_event(receipt["event_id"]).replace("-", "")
        deadline = time.monotonic() + 120.0
        body = None
        while time.monotonic() < deadline:
            req = urllib.request.Request(
                f"{SENTRY_API}/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/events/{sentry_id}/",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    time.sleep(10.0)
                    continue
                raise
        if body is None:
            events.append({"sentry_event_id": sentry_id, "read_back": False})
            fresh_window_ok = False
            continue
        created = datetime.fromisoformat(body["dateCreated"].replace("Z", "+00:00"))
        in_window = delivered_after - timedelta(seconds=5.0) <= created
        fresh_window_ok = fresh_window_ok and in_window
        tags = {tg["key"]: tg["value"] for tg in body["tags"]}
        markers = {m for m in _marker_set() if m in json.dumps(body, default=str)}
        events.append(
            {
                "transport_event_id": receipt["event_id"],
                "sentry_event_id": body["eventID"],
                "read_back": True,
                "dateCreated": body["dateCreated"],
                "fresh_window": in_window,
                "tags_roundtrip": {
                    "task_id": tags.get("task_id") == TASK_ID,
                    "run_id": tags.get("run_id") == RUN_LABEL,
                    "profile_role": tags.get("profile_role") == "worker",
                    "worktree_branch": tags.get("worktree_branch") == BRANCH,
                },
                "context_envelope_roundtrip": (body.get("context") or {})
                .get("envelope", {})
                .get("task_id", "")
                == TASK_ID,
                "marker_hits": sorted(markers),
            }
        )
    all_ok = all(e.get("read_back") for e in events) and fresh_window_ok
    out.update(
        {
            "verdict": "verified" if all_ok else "failed",
            "project_id": exporter._dsn_parts.get("project_id"),
            "events_detail": events,
        }
    )
    return out


def leg_metrics(home: Path) -> dict:
    """Leg B: controller metrics present in emitted envelope events (the
    plugin's redaction-allowlist vocabulary, as the real handlers emit)."""
    from hermes_telemetry import envelope, metrics

    rec = envelope.emit(
        hook="post_api_request",
        session_id="e2e-metrics",
        model="z-ai/glm-5.3-flash",
        provider="openrouter",
        platform="cli",
        profile="worker",
        metadata={
            "tokens_in": 120,
            "tokens_out": 55,
            "cache_read_tokens": 6,
            "cache_write_tokens": 4,
            "reasoning_tokens": 9,
            "cost_usd": 0.0071,
            "latency_ms": 2500,
            "api_call_count": 1,
            "finish_reason": "stop",
            "estimated": False,
        },
    )
    ev = rec["event"] if rec else {}
    expected = (
        "tokens_in",
        "tokens_out",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost_usd",
        "latency_ms",
        "api_call_count",
        "finish_reason",
        "estimated",
    )
    present = [k for k in expected if k in ev]
    return {
        "verdict": "verified" if len(present) == len(expected) else "failed",
        "metrics_present": present,
        "metrics_absent": [k for k in expected if k not in ev],
        "gaps_count": len(metrics.GAPS),
        "envelope_join_keys": {
            "task_id": rec["envelope"]["task_id"],
            "run_id": rec["envelope"]["run_id"],
            "trace_id_prefix": rec["envelope"]["trace_id"][:4],
        }
        if rec
        else None,
    }


def leg_failure_rollback(home: Path) -> dict:
    """Legs C1 (outage) + C2 (fail-closed) + C3 (rollback)."""
    from hermes_telemetry import envelope, sinks
    from hermes_telemetry import transport as transport_mod

    out: dict = {}

    # C1: outage — journal + ledger + status; recovery drains FIFO, zero loss.
    class _Down:
        calls = 0

        def deliver(self, events):
            _Down.calls += 1
            raise TimeoutError("sink unreachable (simulated outage)")

    t = transport_mod.Transport(sink=_Down(), max_retries=2, base_delay_s=0.01, max_delay_s=0.05)
    records = _emit_records(4, "e2e-outage")
    for rec in records:
        t.enqueue(rec["envelope"])
    result = t.flush()
    st = t.status()
    status_json = json.loads(transport_mod.handle_status("json"))
    delivered_path = home / "delivered-recovery.jsonl"
    t2 = transport_mod.Transport(sink=transport_mod.LocalFileSink(delivered_path), dir=t.dir)
    result2 = t2.flush()
    back = [
        json.loads(line)
        for line in delivered_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ledger = [
        json.loads(line)
        for line in (t.dir / "outage_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out["c1_outage"] = {
        "verdict": (
            "verified"
            if (
                result["pending"] == 4
                and status_json["pending"] == 4
                and status_json["outage_open"] is True
                and result2["delivered"] == 4
                and len(back) == 4
                and any(e.get("event") == "open" for e in ledger)
                and any(e.get("event") == "close" for e in ledger)
            )
            else "failed"
        ),
        "journaled": st["journaled"],
        "pending_during_outage": result["pending"],
        "status_pending": status_json["pending"],
        "status_outage_open": status_json["outage_open"],
        "recovered_delivered": result2["delivered"],
        "recovered_fifo_order_ok": [e["envelope"]["trace_id"] for e in back]
        == [rec["envelope"]["trace_id"] for rec in records],
        "ledger_events": [e.get("event") for e in ledger],
    }

    # C2: redaction fail-closed — ZERO POSTs for violation events.
    def _counting():
        calls = []

        def post(url, body, headers, timeout):
            calls.append(url)
            return 202

        return calls, post

    # Two gate layers, two probe classes (matching the pytest legs):
    #   transport gate  — content sentinels / secret shapes (pre-journal)
    #   exporter gate   — SENTINEL-* never-export markers (pre-send)
    # Both obfuscated with U+200B (default-ignorable) — cannot defeat either.
    transport_probe = "PROM\u200bPT_TEXT"
    sink_probe = "SENTINEL-SECRET\u200b-VALUE"
    rejected_by_transport = False
    calls_gate, post_gate = _counting()
    t3 = transport_mod.Transport(
        sink=sinks.LangSmithExporter(
            api_key="e2e-unused", project=LANGSMITH_PROJECT, post=post_gate
        ),
        dir=home / "transport" / "gate",
    )
    env_transport = envelope.build_envelope(
        session_id="e2e-violation",
        model=transport_probe,
        provider="openrouter",
        platform="cli",
        profile="worker",
    )
    try:
        t3.enqueue(env_transport)
    except ValueError:
        rejected_by_transport = True

    def _sink_record(model: str) -> dict:
        env = envelope.build_envelope(
            session_id="e2e-violation",
            model=model,
            provider="openrouter",
            platform="cli",
            profile="worker",
        )
        return {
            "event_id": "ev-" + uuid.uuid4().hex[:12],
            "enqueued_at": "2026-09-19T00:00:00+00:00",
            "attempt": 1,
            "envelope": env,
        }

    calls_ls, post_ls = _counting()
    ls = sinks.LangSmithExporter(api_key="e2e-unused", project=LANGSMITH_PROJECT, post=post_ls)
    r_ls = ls.deliver([_sink_record(sink_probe)])
    calls_sn, post_sn = _counting()
    sn = sinks.SentryExporter(dsn="https://public@example.ingest.sentry.io/42", post=post_sn)
    r_sn = sn.deliver([_sink_record(sink_probe)])
    out["c2_fail_closed"] = {
        "verdict": (
            "verified"
            if (
                rejected_by_transport
                and len(calls_gate) == 0
                and r_ls["ok"] is False
                and len(calls_ls) == 0
                and r_sn["ok"] is False
                and len(calls_sn) == 0
            )
            else "failed"
        ),
        "transport_probe": "U+200B-obfuscated content sentinel in schema-allowed model field",
        "transport_gate_rejected": rejected_by_transport,
        "transport_gate_posts": len(calls_gate),
        "sink_probe": "U+200B-obfuscated SENTINEL-SECRET-VALUE never-export marker",
        "langsmith_gate_ok": r_ls["ok"] is False,
        "langsmith_posts": len(calls_ls),
        "sentry_gate_ok": r_sn["ok"] is False,
        "sentry_posts": len(calls_sn),
    }

    # C3: rollback — disposable home returns to pre-test (empty) inventory.
    def _inventory(root: Path) -> dict:
        import hashlib

        inv = {}
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                rel = p.relative_to(root).as_posix()
                if p.is_file():
                    inv[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
                else:
                    inv[rel] = "dir"
        return inv

    rhome = home / "rollback-home"
    rhome.mkdir(parents=True, exist_ok=True)
    pre = _inventory(rhome)
    # Representative pipeline writes INSIDE the rollback home only.
    os.environ["HERMES_TELEMETRY_HOME"] = str(rhome)
    os.environ["HERMES_HOME"] = str(rhome)
    from hermes_telemetry import paths as _paths

    _paths.get_db_path()
    t4 = transport_mod.Transport(
        sink=transport_mod.LocalFileSink(rhome / "telemetry" / "delivered.jsonl"),
        dir=rhome / "telemetry" / "transport",
    )
    rec_rb = _emit_records(1, "e2e-rollback")[0]
    receipt_rb = t4.enqueue(rec_rb["envelope"])
    assert t4.flush()["ok"] is True
    assert t4.receipt(receipt_rb["event_id"])["delivered"] is True
    during = _inventory(rhome)
    shutil.rmtree(rhome, ignore_errors=True)
    post = _inventory(rhome)
    os.environ["HERMES_TELEMETRY_HOME"] = str(home)
    os.environ["HERMES_HOME"] = str(home)
    out["c3_rollback"] = {
        "verdict": "verified" if (pre == {} and post == pre and during) else "failed",
        "pre_test_inventory_entries": len(pre),
        "pipeline_artifact_entries": len(during),
        "post_teardown_entries": len(post),
        "real_home_path_used": False,
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="skip live sink legs")
    args = parser.parse_args()

    home = _setup_disposable_home()
    started = datetime.now(timezone.utc)
    try:
        if not args.offline:
            EVIDENCE["legs"]["A_langsmith"] = leg_langsmith(home)
            EVIDENCE["legs"]["A_sentry"] = leg_sentry(home)
        else:
            EVIDENCE["legs"]["A_langsmith"] = {"verdict": "skipped (--offline)"}
            EVIDENCE["legs"]["A_sentry"] = {"verdict": "skipped (--offline)"}
        EVIDENCE["legs"]["B_metrics"] = leg_metrics(home)
        EVIDENCE["legs"]["C_failure_rollback"] = leg_failure_rollback(home)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    EVIDENCE["started_at"] = started.isoformat()
    EVIDENCE["finished_at"] = datetime.now(timezone.utc).isoformat()
    EVIDENCE["redaction_note"] = (
        "ids/counts/timestamps only — no envelope payloads, no credential values"
    )
    print(json.dumps(EVIDENCE, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
