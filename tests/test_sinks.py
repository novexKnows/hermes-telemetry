"""tests/test_sinks.py — B4 sandbox exporters (LangSmith + Sentry).

Unit tests run against an injected transport (no network, no credentials).
The real sandbox legs are integration tests that SKIP unless the credential
env vars are present (contract §3.4: credentials enter via env only; absence
means skip, never a fabricated flow).

HERMES_HOME isolation comes from the project-level autouse conftest fixture.
Credential env vars are cleared per test so a developer's ambient shell state
cannot turn a unit test into a network call or flip config paths.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import uuid

import hermes_telemetry.sinks as sinks
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CREDENTIAL_ENV_VARS = [
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_ENDPOINT",
    "LANGSMITH_PROJECT",
    "LANGCHAIN_PROJECT",
    "LANGSMITH_WORKSPACE_ID",
    "LANGCHAIN_WORKSPACE_ID",
    "LANGSMITH_TENANT_ID",
    "LANGCHAIN_TENANT_ID",
    "SENTRY_DSN",
]

# Sentinel markers (telemetry-contract.md §3.3 redaction-leakage scan): if any
# of these EVER appear in an exported payload, the export path leaks.
MARKERS = [
    "SENTINEL-PROMPT-TEXT",
    "SENTINEL-RESPONSE-TEXT",
    "SENTINEL-TOOL-RESULT-BODY",
    "SENTINEL-SECRET-VALUE",
    "SENTINEL-ENV-VALUE",
    "SENTINEL-FILE-CONTENT",
]


@pytest.fixture(autouse=True)
def credential_env_snapshot_and_clear(monkeypatch):
    """Snapshot ambient credential env, then clear it for EVERY test.

    Unit tests must never see ambient credentials (no accidental network, no
    config drift). Integration tests request the snapshot and re-apply it via
    monkeypatch so the real leg still runs — the secret only ever lives in the
    process env, never in a fixture attribute, log or report.
    """
    ambient = {name: os.environ[name] for name in CREDENTIAL_ENV_VARS if name in os.environ}
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return ambient


def make_envelope(trace_id: str | None = None) -> dict:
    """A schema-shaped harness envelope (base fields; extensions null).

    Field set mirrors B1's ``build_envelope`` output; this branch does not
    import envelope.py (B1 module — merge-order independence), it only
    produces an equivalent dict.
    """
    return {
        "board": "sovav-hermes",
        "task_id": "t_763d8d58",
        "run_id": 11,
        "parent_task_ids": [],
        "profile/role": "worker",
        "model": "z-ai/glm-5.3-flash",
        "provider": "openrouter",
        "key_id": "unlabeled",
        "call_class": "aux",
        "handoff_id": None,
        "worktree/branch": "wt/htb-b4",
        "trace_id": trace_id or str(uuid.uuid4()),
        "repo": None,
        "agent_run_id": None,
        "correlation_id": None,
        "idempotency_key": None,
        "deal_id": None,
        "worker_id": None,
        "model_ref": None,
        "trigger_type": None,
        "runtime_environment": None,
        "basis_fingerprint": None,
        "worker_definition_version": None,
        "output_schema_version": None,
        "harness_task_id": None,
        "harness_run_id": None,
    }


def make_event(
    seq: int = 0,
    envelope: dict | None = None,
    enqueued_at: str = "2026-09-19T15:00:01.234567+00:00",
):
    return {
        "event_id": f"ev-{seq:04d}",
        "enqueued_at": enqueued_at,
        "attempt": 1,
        "envelope": envelope or make_envelope(),
    }


class RecordingPost:
    """Injectable transport standing in for HTTP; records every call."""

    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[tuple[str, dict, dict]] = []
        self.fail_on = fail_on or set()

    def __call__(self, url: str, body: dict, headers: dict, timeout: float) -> int:
        self.calls.append((url, body, headers))
        event_key = body.get("extra", {}).get("metadata", {}).get("event_id") or body.get(
            "tags", {}
        ).get("event_id")
        if event_key in self.fail_on:
            raise ConnectionError("injected transport failure")
        return 200


# ---------------------------------------------------------------------------
# LangSmithExporter — unit (mock transport, no network)
# ---------------------------------------------------------------------------


def test_langsmith_happy_path_payload_shape():
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    events = [make_event(1), make_event(2)]
    result = exp.deliver(events)

    assert result["ok"] is True
    assert result["accepted"] == 2
    assert len(post.calls) == 2
    url, body, headers = post.calls[0]
    assert url == f"{sinks.LangSmithExporter.DEFAULT_API_URL}/runs"
    assert headers["x-api-key"] == "test-key"  # unit fixture value, not a secret
    # Metadata-only shape: finished run, empty inputs/outputs.
    assert body["name"] == sinks.LangSmithExporter.RUN_NAME
    assert body["run_type"] == sinks.LangSmithExporter.RUN_TYPE
    assert body["inputs"] == {}
    assert body["outputs"] == {}
    assert body["start_time"] == make_event(1)["enqueued_at"]
    assert body["end_time"] == make_event(1)["enqueued_at"]
    # Envelope attached VERBATIM (§1.6: slash keys are legal in LangSmith metadata).
    assert body["extra"]["metadata"]["profile/role"] == "worker"
    assert body["extra"]["metadata"]["worktree/branch"] == "wt/htb-b4"
    assert body["extra"]["metadata"]["run_id"] == 11
    assert body["extra"]["metadata"]["event_id"] == "ev-0001"
    # Project default is the TM-dispatched sandbox project.
    assert body["session_name"] == "sovav-hermes-sandbox"


def test_langsmith_run_id_deterministic_and_idempotent():
    rid = sinks.langsmith_run_id_for_event("ev-0001")
    assert rid == sinks.langsmith_run_id_for_event("ev-0001")
    assert rid != sinks.langsmith_run_id_for_event("ev-0002")
    # The run payload carries exactly that id (read-back join key).
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    exp.deliver([make_event(1)])
    assert post.calls[0][1]["id"] == rid


def test_langsmith_redaction_gate_rejects_non_pinned_keys():
    """An event carrying non-pinned keys (e.g. a prompt) must NOT be forwarded."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    leaked = make_event(1)
    leaked["prompt"] = MARKERS[0]  # never-export material smuggled at record level
    result = exp.deliver([leaked, make_event(2)])

    assert result["ok"] is False
    assert result["accepted"] == 0
    assert post.calls == []  # nothing forwarded
    assert "redaction gate" in result["detail"]
    assert MARKERS[0] not in result["detail"]  # names only, never values


def test_langsmith_exported_payloads_never_contain_marker_material():
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    envelopes = [make_envelope() for _ in range(3)]
    events = [make_event(i, envelope=envelopes[i]) for i in range(3)]
    result = exp.deliver(events)

    assert result["ok"] is True
    exported = json.dumps([body for _url, body, _h in post.calls])
    for marker in MARKERS:
        assert marker not in exported


# ---------------------------------------------------------------------------
# Adversarial negatives: banned material hidden inside an ALLOWED envelope
# value via Unicode default-ignorable obfuscation. The pre-send gate must
# normalize-then-match over the actual outbound payload: reject with NO POST,
# and never echo the value (raw or normalized) in detail. (R4a fix.)
# ---------------------------------------------------------------------------


def _envelope_with_model(value: str) -> dict:
    """A schema-shaped envelope whose allowed ``model`` field carries the attack."""
    env = make_envelope()
    env["model"] = value
    return env


@pytest.mark.parametrize(
    "obfuscated",
    [
        "SEN\u2063TINEL-SECRET-VALUE",  # U+2063 invisible separator (R4a probe)
        "SENTINEL\u200b-SECRET-VALUE",  # U+200B zero width space
        "SEN\u2063TINEL\u2063-SECRET\u200b-VALUE",  # doubled / mixed
        "SEN\U000e0020TINEL-SECRET-VALUE",  # U+E0020 tag character
    ],
)
def test_langsmith_gate_rejects_obfuscated_sentinel_in_envelope_value_without_post(
    obfuscated,
):
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    result = exp.deliver([make_event(1, envelope=_envelope_with_model(obfuscated))])

    assert result["ok"] is False
    assert result["accepted"] == 0
    assert post.calls == []  # zero network calls: nothing was POSTed
    # Violations name locations only — neither the raw obfuscated value nor
    # its normalized (de-obfuscated) form may appear in detail.
    assert obfuscated not in result["detail"]
    assert sinks._strip_default_ignorables(obfuscated) not in result["detail"]


def test_langsmith_gate_rejects_plain_sentinel_in_envelope_value_without_post():
    """Not obfuscated at all — the plain marker in an allowed value also fails."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    result = exp.deliver([make_event(1, envelope=_envelope_with_model(MARKERS[3]))])

    assert result["ok"] is False
    assert result["accepted"] == 0
    assert post.calls == []
    assert MARKERS[3] not in result["detail"]


def test_langsmith_gate_rejects_secret_shape_in_envelope_value_without_post():
    """Secret SHAPES (not just markers) are banned too — synthetic fixture key."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    fake = "sk-FAKE-unit-fixture-0123456789"
    result = exp.deliver([make_event(1, envelope=_envelope_with_model(fake))])

    assert result["ok"] is False
    assert result["accepted"] == 0
    assert post.calls == []
    assert fake not in result["detail"]


def test_langsmith_make_run_violation_for_obfuscated_marker():
    """The gate is enforced at payload construction: make_run refuses."""
    exp = sinks.LangSmithExporter(api_key="test-key")
    payload, violation = exp.make_run(
        make_event(1, envelope=_envelope_with_model("SEN\u2063TINEL-SECRET-VALUE"))
    )
    assert payload == {}
    assert violation is not None
    assert "SENTINEL-SECRET-VALUE" not in violation


def test_langsmith_gate_stops_batch_at_first_leak_attempt():
    """A leaking event halts the batch: later events are never forwarded."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    leak = make_event(1, envelope=_envelope_with_model("SENTINEL\u2063-PROMPT-TEXT"))
    result = exp.deliver([leak, make_event(2), make_event(3)])

    assert result["ok"] is False
    assert result["accepted"] == 0
    assert len(post.calls) == 0
    assert "SENTINEL-PROMPT-TEXT" not in result["detail"]
    assert "SENTINEL\u2063" not in result["detail"]


def test_langsmith_gate_strips_ignorables_from_clean_values_before_send():
    """A benign value carrying an invisible character is not banned material:
    it is delivered with the ignorable already stripped (normalize-before-
    send), so nothing invisible survives to the vendor."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    result = exp.deliver([make_event(1, envelope=_envelope_with_model("gpt\u200b-4o"))])

    assert result["ok"] is True
    assert len(post.calls) == 1
    assert post.calls[0][1]["extra"]["metadata"]["model"] == "gpt-4o"


def test_langsmith_gate_rejects_obfuscated_sentinel_as_envelope_key_without_post():
    """R4a2 key-negative: the U+2063-obfuscated sentinel used AS an envelope
    JSON key must fail closed with zero POSTs, and the key text must appear
    NOWHERE in detail — neither the raw obfuscated form nor its normalized
    (de-obfuscated) form. The reported location is opaque (positional)."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    raw_key = "SEN\u2063TINEL-SECRET-VALUE"
    env = make_envelope()
    env[raw_key] = "innocuous"
    result = exp.deliver([make_event(1, envelope=env)])

    assert result["ok"] is False
    assert result["accepted"] == 0
    assert post.calls == []  # zero network calls: nothing was POSTed
    assert raw_key not in result["detail"]
    assert sinks._strip_default_ignorables(raw_key) not in result["detail"]
    assert "SENTINEL-SECRET-VALUE" not in result["detail"]
    # Opaque location: a positional key label, never attacker-controlled text.
    assert "key[" in result["detail"]


def test_langsmith_gate_rejects_plain_banned_key_without_post():
    """Direct (non-obfuscated) banned key also fails closed with a clean detail."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    env = make_envelope()
    env["SENTINEL-SECRET-VALUE"] = "innocuous"
    result = exp.deliver([make_event(1, envelope=env)])

    assert result["ok"] is False
    assert result["accepted"] == 0
    assert post.calls == []
    assert "SENTINEL-SECRET-VALUE" not in result["detail"]
    assert "key[" in result["detail"]


def test_langsmith_partial_accept_is_in_order():
    """Second event fails -> only the first counts accepted (in-order prefix)."""
    post = RecordingPost(fail_on={"ev-0002"})
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    result = exp.deliver([make_event(1), make_event(2), make_event(3)])

    assert result["ok"] is False
    assert result["accepted"] == 1
    assert len(post.calls) == 2  # third event never attempted
    assert "ev-0002: ConnectionError" in result["detail"]


def test_langsmith_dedupe_counts_accepted_without_retransmit():
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    event = make_event(1)
    result = exp.deliver([event, dict(event)])

    assert result["ok"] is True
    assert result["accepted"] == 2
    assert len(post.calls) == 1  # duplicate not re-transmitted
    assert "deduped 1" in result["detail"]


def test_langsmith_missing_credentials_returns_not_ok_without_network():
    post = RecordingPost()
    exp = sinks.LangSmithExporter(post=post)
    assert not exp.api_key
    result = exp.deliver([make_event(1)])
    assert result["ok"] is False
    assert "LANGSMITH_API_KEY" in result["detail"]
    assert post.calls == []


def test_langsmith_kill_switch_enabled_false():
    exp = sinks.LangSmithExporter(api_key="test-key", enabled=False)
    result = exp.deliver([make_event(1)])
    assert result["ok"] is False
    assert "disabled" in result["detail"]


def test_langsmith_env_credential_and_project_resolution(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "env-key")  # unit fixture value
    monkeypatch.setenv("LANGSMITH_PROJECT", "custom-project")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://example.invalid")
    exp = sinks.LangSmithExporter()
    assert exp.api_key == "env-key"
    assert exp.project == "custom-project"
    assert exp.api_url == "https://example.invalid"


def test_langsmith_env_fallbacks_langchain_names(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "legacy-key")  # unit fixture value
    exp = sinks.LangSmithExporter()
    assert exp.api_key == "legacy-key"


# ---------------------------------------------------------------------------
# LangSmithExporter — tenant/workspace resolution (X-Tenant-Id)
# ---------------------------------------------------------------------------


class RecordingGet:
    """Injectable GET transport standing in for tenant discovery."""

    def __init__(self, workspaces: list[dict] | None = None, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.workspaces = (
            workspaces
            if workspaces is not None
            else [
                {"id": "ws-1", "is_deleted": False},
                {"id": "ws-2", "is_deleted": True},
            ]
        )
        self.fail = fail

    def __call__(self, url: str, headers: dict, timeout: float) -> tuple[int, str]:
        self.calls.append((url, headers))
        if self.fail:
            raise ConnectionError("injected discovery failure")
        return 200, json.dumps(self.workspaces)


def test_langsmith_tenant_header_from_env(monkeypatch):
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", "ws-env-1")
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    assert exp.workspace_id == "ws-env-1"  # tenant metadata, not a secret
    result = exp.deliver([make_event(1)])
    assert result["ok"] is True
    assert post.calls[0][2]["X-Tenant-Id"] == "ws-env-1"


def test_langsmith_tenant_discovery_sets_header_once():
    get = RecordingGet()
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post, get=get)
    result = exp.deliver([make_event(1), make_event(2)])
    assert result["ok"] is True
    assert exp.workspace_id == "ws-1"  # first non-deleted workspace
    # Discovery happened once and used the pinned endpoint + key header.
    assert len(get.calls) == 1
    url, headers = get.calls[0]
    assert url == f"{sinks.LangSmithExporter.DEFAULT_API_URL}/api/v1/workspaces"
    assert headers == {"x-api-key": "test-key"}
    # Every write carries the discovered tenant id.
    assert all(h["X-Tenant-Id"] == "ws-1" for _u, _b, h in post.calls)


def test_langsmith_tenant_discovery_failure_fails_closed_without_writes():
    get = RecordingGet(fail=True)
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post, get=get)
    result = exp.deliver([make_event(1)])
    assert result["ok"] is False
    assert "tenant resolution failed" in result["detail"]
    assert post.calls == []  # fail closed: without tenant scoping writes 403
    assert "test-key" not in result["detail"]  # names only, never values


def test_langsmith_injected_post_without_get_skips_discovery():
    """Backward compat: custom write transport + no get -> no discovery call."""
    post = RecordingPost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=post)
    assert exp._get is None
    result = exp.deliver([make_event(1)])
    assert result["ok"] is True
    assert "X-Tenant-Id" not in post.calls[0][2]


class DuplicateCreatePost(RecordingPost):
    """Transport mimicking LangSmith's one-create-per-run-id rule (409 on re-POST)."""

    def __call__(self, url: str, body: dict, headers: dict, timeout: float) -> int:
        event_key = body.get("extra", {}).get("metadata", {}).get("event_id")
        if event_key in self.calls_seen:
            raise urllib.error.HTTPError(
                url=url, code=409, msg="Run create payload already received", hdrs=None, fp=None
            )
        self.calls_seen.add(event_key)
        return super().__call__(url, body, headers, timeout)

    def __init__(self, fail_on: set[str] | None = None):
        super().__init__(fail_on)
        self.calls_seen: set[str] = set()


def test_langsmith_server_duplicate_create_counts_accepted():
    """409 on re-POST (retry across exporter instances) is accepted-duplicate."""
    first = DuplicateCreatePost()
    exp = sinks.LangSmithExporter(api_key="test-key", post=first)
    result = exp.deliver([make_event(1)])
    assert result["ok"] is True

    retry = DuplicateCreatePost()  # fresh exporter, server already has the run
    retry.calls_seen.add("ev-0001")
    exp2 = sinks.LangSmithExporter(api_key="test-key", post=retry)
    result2 = exp2.deliver([make_event(1)])
    assert result2["ok"] is True
    assert result2["accepted"] == 1
    assert "deduped 1" in result2["detail"]


# ---------------------------------------------------------------------------
# SentryExporter — unit (mock transport, no network)
# ---------------------------------------------------------------------------


FAKE_DSN = "https://examplePublicKey@exampleoops.ingest.sentry.io/1234567"


def test_sentry_happy_path_tag_mapping():
    post = RecordingPost()
    exp = sinks.SentryExporter(dsn=FAKE_DSN, environment="dev", post=post)
    result = exp.deliver([make_event(1)])

    assert result["ok"] is True
    assert result["accepted"] == 1
    url, body, headers = post.calls[0]
    assert url == "https://exampleoops.ingest.sentry.io/api/1234567/store/"
    # Contract §1.6: slash keys normalized to underscores in tag keys.
    assert body["tags"]["profile_role"] == "worker"
    assert body["tags"]["worktree_branch"] == "wt/htb-b4"
    assert body["tags"]["event_id"] == "ev-0001"
    # run_id serialized as a string in tag values.
    assert body["tags"]["run_id"] == "11"
    # Envelope also carried verbatim in extra (metadata-only, no content).
    assert body["extra"]["envelope"]["profile/role"] == "worker"
    assert body["level"] == "info"
    assert "examplePublicKey" not in json.dumps(body)  # key never in payload


def test_sentry_no_dsn_returns_not_ok_without_network():
    post = RecordingPost()
    exp = sinks.SentryExporter(post=post)
    result = exp.deliver([make_event(1)])
    assert result["ok"] is False
    assert "SENTRY_DSN" in result["detail"]
    assert post.calls == []


def test_sentry_invalid_dsn_returns_not_ok():
    exp = sinks.SentryExporter(dsn="not-a-dsn")
    assert exp.configured is False
    result = exp.deliver([make_event(1)])
    assert result["ok"] is False


def test_sentry_redaction_gate_rejects_non_pinned_keys():
    post = RecordingPost()
    exp = sinks.SentryExporter(dsn=FAKE_DSN, post=post)
    leaked = make_event(1)
    leaked["tool_result"] = MARKERS[2]
    result = exp.deliver([leaked])
    assert result["ok"] is False
    assert post.calls == []
    assert MARKERS[2] not in result["detail"]


def test_sentry_partial_accept_and_dedupe():
    post = RecordingPost(fail_on={"ev-0001"})
    exp = sinks.SentryExporter(dsn=FAKE_DSN, post=post)
    result = exp.deliver([make_event(1), make_event(2)])
    assert result["ok"] is False
    assert result["accepted"] == 0

    post2 = RecordingPost()
    exp2 = sinks.SentryExporter(dsn=FAKE_DSN, post=post2)
    event = make_event(1)
    result2 = exp2.deliver([event, dict(event)])
    assert result2["ok"] is True
    assert result2["accepted"] == 2
    assert len(post2.calls) == 1


def test_parse_sentry_dsn_components():
    parts = sinks.parse_sentry_dsn(FAKE_DSN)
    assert parts == {
        "scheme": "https",
        "public_key": "examplePublicKey",
        "host": "exampleoops.ingest.sentry.io",
        "project_id": "1234567",
    }
    assert sinks.parse_sentry_dsn("") is None
    assert sinks.parse_sentry_dsn("https://no-project-id@host/") is None


def test_sentry_kill_switch_enabled_false():
    exp = sinks.SentryExporter(dsn=FAKE_DSN, enabled=False)
    result = exp.deliver([make_event(1)])
    assert result["ok"] is False
    assert "disabled" in result["detail"]


# ---------------------------------------------------------------------------
# Integration — real sandbox legs (skip without credentials; NEVER fabricated)
# ---------------------------------------------------------------------------


def _has_ambient_langsmith_creds(ambient: dict) -> bool:
    return bool(ambient.get("LANGSMITH_API_KEY") or ambient.get("LANGCHAIN_API_KEY"))


def _readback_with_retry(client, project: str, run_ids: list[str], timeout_s: float = 60.0) -> list:
    """Poll the SDK read path until every run id is visible (ingestion lag).

    LangSmith indexes runs asynchronously: a run-create returns 202 long
    before ``list_runs`` can see it (observed lag: a few seconds). Bounded
    retry — if the deadline passes with runs missing, the (partial) result is
    returned and the caller's assertions fail on the REAL state, never a
    fabricated success.
    """
    deadline = time.monotonic() + timeout_s
    back: list = []
    while True:
        back = list(client.list_runs(project_name=project, run_ids=run_ids))
        if len(back) == len(run_ids) or time.monotonic() >= deadline:
            return back
        time.sleep(3.0)


def test_integration_langsmith_sandbox_end_to_end(credential_env_snapshot_and_clear, monkeypatch):
    """Real metadata-only sandbox flow: deliver -> SDK read-back proves arrival.

    Requires LANGSMITH_API_KEY in the ambient env (loaded directly or by the
    conftest runtime-credential loader from the approved key file; sandbox
    project sovav-hermes-sandbox, or LANGSMITH_PROJECT override). Tenant
    scoping: the write path discovers the workspace id automatically; the SDK
    read path gets the same id via ``Client(workspace_id=...)``. Uses the
    official ``langsmith`` SDK READ path only; the write path is the exporter
    itself. Skips (not fails) without credentials — the no-fabrication gate.
    """
    langsmith = pytest.importorskip("langsmith")
    ambient = credential_env_snapshot_and_clear
    if not _has_ambient_langsmith_creds(ambient):
        pytest.skip("no LangSmith credentials in ambient env (LANGSMITH_API_KEY absent)")
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)

    project = os.environ.get("LANGSMITH_PROJECT") or sinks.LangSmithExporter.DEFAULT_PROJECT
    events = [
        make_event(901),
        make_event(902),
        make_event(903, enqueued_at="2026-09-19T15:00:05.000000+00:00"),
    ]
    run_ids = [sinks.langsmith_run_id_for_event(e["event_id"]) for e in events]

    exp = sinks.LangSmithExporter()
    result = exp.deliver(events)
    assert result["ok"] is True, result["detail"]
    assert result["accepted"] == 3

    client = langsmith.Client(workspace_id=exp.workspace_id)  # key comes from env itself
    back = _readback_with_retry(client, project, run_ids)
    assert len(back) == 3
    got_event_ids = {r.extra.get("metadata", {}).get("event_id") for r in back}
    assert got_event_ids == {"ev-0901", "ev-0902", "ev-0903"}
    # Join-key spot-check: envelope survives round-trip verbatim.
    for run in back:
        md = run.extra.get("metadata", {})
        assert md["profile/role"] == "worker"
        assert md["worktree/branch"] == "wt/htb-b4"
        assert md["task_id"] == "t_763d8d58"


def test_integration_langsmith_readback_is_metadata_only(
    credential_env_snapshot_and_clear, monkeypatch
):
    """Negative redaction scan over the REAL read-back payloads (§3.3)."""
    langsmith = pytest.importorskip("langsmith")
    ambient = credential_env_snapshot_and_clear
    if not _has_ambient_langsmith_creds(ambient):
        pytest.skip("no LangSmith credentials in ambient env (LANGSMITH_API_KEY absent)")
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)

    project = os.environ.get("LANGSMITH_PROJECT") or sinks.LangSmithExporter.DEFAULT_PROJECT
    event = make_event(904)
    exp = sinks.LangSmithExporter()
    result = exp.deliver([event])
    assert result["ok"] is True

    client = langsmith.Client(workspace_id=exp.workspace_id)
    back = _readback_with_retry(
        client, project, [sinks.langsmith_run_id_for_event(event["event_id"])]
    )
    assert len(back) == 1
    run = back[0]
    # No prompt/response/tool-result material anywhere in the round-tripped run.
    assert run.inputs == {} or run.inputs is None
    assert run.outputs == {} or run.outputs is None
    serialized = json.dumps(run.dict(), default=str)
    for marker in MARKERS:
        assert marker not in serialized
