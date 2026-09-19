"""tests/test_seer.py — B4b Seer client (setup check, preferences, analysis loop).

Unit tests run against an injected transport (no network, no credentials).
The live Seer leg is an integration test that SKIPS unless ``SENTRY_AUTH_TOKEN``
is present in the ambient env (contract §3.4: credentials enter via env only;
absence means skip, never a fabricated flow).

HERMES_HOME isolation comes from the project-level autouse conftest fixture.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import seer as seer_module
import sinks as sinks_module

SENTRY_CREDENTIAL_ENV_VARS = ["SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT"]

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
    ambient = {name: os.environ[name] for name in SENTRY_CREDENTIAL_ENV_VARS if name in os.environ}
    for name in SENTRY_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return ambient


class RecordingTransport:
    """Injectable transport standing in for HTTP; records and replays."""

    def __init__(self, responses: dict[tuple[str, str], tuple[int, dict]] | None = None):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.responses = responses or {}

    def __call__(self, url, method, headers, body, timeout):
        self.calls.append((url, method, headers, body))
        key = (method, url)
        if key in self.responses:
            status, payload = self.responses[key]
            return status, json.dumps(payload).encode("utf-8")
        return 200, b"{}"


def make_client(transport) -> seer_module.SeerClient:
    return seer_module.SeerClient(api_token="test-token", request=transport)  # unit fixture value


# ---------------------------------------------------------------------------
# Unit — no network
# ---------------------------------------------------------------------------


def test_seer_no_token_returns_not_ok_without_network():
    transport = RecordingTransport()
    client = seer_module.SeerClient(request=transport)
    assert client.configured is False
    result = client.setup_check()
    assert result["ok"] is False
    assert "SENTRY_AUTH_TOKEN" in result["detail"]
    assert transport.calls == []  # nothing sent


def test_seer_setup_check_url_and_auth_header():
    transport = RecordingTransport()
    client = make_client(transport)
    result = client.setup_check()
    assert result["ok"] is True
    url, method, headers, _body = transport.calls[0]
    assert url == "https://sentry.io/api/0/organizations/sovav/seer/setup-check/"
    assert method == "GET"
    # Header carries the token (unit fixture value); the value never leaks
    # into the result dict.
    assert headers["Authorization"] == "Bearer test-token"
    assert "test-token" not in json.dumps(result)


def test_seer_project_preferences_path():
    transport = RecordingTransport()
    client = make_client(transport)
    client.project_preferences()
    url, method, _h, _b = transport.calls[0]
    assert url == "https://sentry.io/api/0/projects/sovav/sovav-hermes-sandbox/seer/preferences/"
    assert method == "GET"


def test_seer_summarize_issue_post_shape():
    transport = RecordingTransport(
        responses={
            (
                "POST",
                "https://sentry.io/api/0/organizations/sovav/issues/42/summarize/",
            ): (
                200,
                {
                    "groupId": "42",
                    "headline": "H",
                    "whatsWrong": "W",
                    "possibleCause": "C",
                    "scores": {"possibleCauseConfidence": 0.4},
                },
            )
        }
    )
    client = make_client(transport)
    result = client.summarize_issue("42")
    assert result["ok"] is True
    url, method, headers, body = transport.calls[0]
    assert url.endswith("/organizations/sovav/issues/42/summarize/")
    assert method == "POST"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {}
    assert result["body"]["headline"] == "H"
    assert result["body"]["scores"]["possibleCauseConfidence"] == 0.4


def test_seer_summarize_issue_forced_event_post_shape():
    """event_id maps to the endpoint's force_event_id (verified against live
    getsentry source) so the summary provably belongs to THAT event."""
    transport = RecordingTransport(
        responses={
            (
                "POST",
                "https://sentry.io/api/0/organizations/sovav/issues/42/summarize/",
            ): (200, {"groupId": "42", "eventId": "e" * 32, "headline": "H"})
        }
    )
    client = make_client(transport)
    result = client.summarize_issue("42", event_id="e" * 32)
    assert result["ok"] is True
    url, method, headers, body = transport.calls[0]
    assert url.endswith("/organizations/sovav/issues/42/summarize/")
    assert method == "POST"
    assert json.loads(body) == {"event_id": "e" * 32}
    # Without event_id the body stays the empty-object default.
    transport2 = RecordingTransport()
    make_client(transport2).summarize_issue("42")
    _url, _method, _h, body2 = transport2.calls[0]
    assert json.loads(body2) == {}


def test_seer_trigger_autofix_surfaces_409_verbatim():
    transport = RecordingTransport(
        responses={
            (
                "POST",
                "https://sentry.io/api/0/organizations/sovav/issues/42/autofix/",
            ): (409, {"detail": "repos required", "code": "repos_not_linked"})
        }
    )
    client = make_client(transport)
    result = client.trigger_autofix("42")
    assert result["ok"] is False
    assert result["status"] == 409
    assert result["body"]["code"] == "repos_not_linked"  # verbatim, never raised


def test_seer_autofix_status_get_path():
    transport = RecordingTransport()
    client = make_client(transport)
    client.autofix_status(42)
    url, method, _h, _b = transport.calls[0]
    assert url.endswith("/organizations/sovav/issues/42/autofix/")
    assert method == "GET"


def test_seer_env_token_and_org_resolution(monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "env-token")  # unit fixture value
    monkeypatch.setenv("SENTRY_ORG", "custom-org")
    monkeypatch.setenv("SENTRY_PROJECT", "custom-project")
    client = seer_module.SeerClient()
    assert client.api_token == "env-token"
    assert client.org == "custom-org"
    assert client.project == "custom-project"


def test_seer_transport_failure_normalized_not_raised():
    def boom(url, method, headers, body, timeout):
        raise ConnectionError("injected transport failure")

    client = seer_module.SeerClient(api_token="test-token", request=boom)  # unit fixture value
    result = client.setup_check()
    assert result["ok"] is False
    assert result["status"] is None
    assert result["detail"] == "ConnectionError"


# ---------------------------------------------------------------------------
# Integration — live Seer leg (skip without credentials; NEVER fabricated)
# ---------------------------------------------------------------------------

SENTRY_API = "https://sentry.io/api/0"
SENTRY_READBACK_DEADLINE_S = 120.0
SENTRY_READBACK_POLL_S = 10.0


def _sentry_event_id_for(event_id: str) -> str:
    """The Sentry event id the exporter assigned for a transport event id."""
    return sinks_module.langsmith_run_id_for_event(event_id).replace("-", "")


def _sentry_api_get(path: str, token: str) -> tuple[int, object]:
    from urllib import request as urlreq

    req = urlreq.Request(f"{SENTRY_API}{path}", headers={"Authorization": f"Bearer {token}"})
    with urlreq.urlopen(req, timeout=15) as resp:
        return int(resp.status), json.load(resp)


def _poll_sentry_event(token: str, org: str, project: str, sentry_event_id: str) -> dict:
    """Poll the per-event read-back until it appears (or DeadlineExceeded).

    Read-back of the exact per-run id is what proves FRESH ingestion — an
    older server object cannot answer for a fresh id (the R4b reviewer
    reproduced the fixed-id failure mode).
    """
    import time
    from urllib import error as urlerror

    deadline = time.monotonic() + SENTRY_READBACK_DEADLINE_S
    while time.monotonic() < deadline:
        try:
            status, body = _sentry_api_get(
                f"/projects/{org}/{project}/events/{sentry_event_id}/", token
            )
        except urlerror.HTTPError as exc:
            if exc.code == 404:  # not yet processed — keep polling
                time.sleep(SENTRY_READBACK_POLL_S)
                continue
            raise
        return body
    raise AssertionError(
        f"event {sentry_event_id} not readable after {SENTRY_READBACK_DEADLINE_S:.0f}s"
    )


# Fresh-object timestamp proof (R4b2 round-3 MAJOR finding): Sentry's API
# ``dateCreated`` must fall inside THIS run's delivery window. Small allowance
# for API timestamp precision / clock skew on either side.
SENTRY_DATECREATED_SKEW_S = 5.0


def _assert_fresh_datecreated(body: dict, delivered_after: datetime) -> datetime:
    """Assert the read-back event's ``dateCreated`` is within the fresh window.

    ``delivered_after`` is the UTC timestamp captured immediately BEFORE
    delivery; the API creation timestamp must be >= it (minus the skew
    allowance) and <= now (plus it). A reused older event cannot pass this.
    """
    raw = body.get("dateCreated")
    assert isinstance(raw, str) and raw, f"missing/invalid dateCreated in read-back: {raw!r}"
    created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    lower = delivered_after - timedelta(seconds=SENTRY_DATECREATED_SKEW_S)
    upper = datetime.now(timezone.utc) + timedelta(seconds=SENTRY_DATECREATED_SKEW_S)
    assert lower <= created <= upper, (
        f"dateCreated {created.isoformat()} outside fresh window "
        f"[{lower.isoformat()}, {upper.isoformat()}] — object is not from this run"
    )
    return created


def test_integration_seer_sandbox_analysis_loop(credential_env_snapshot_and_clear, monkeypatch):
    """Real Seer loop: setup-check -> ingest a FRESH metadata-only event ->
    read back THAT exact event -> join its issue group by the event's own
    group id (never by the shared title) -> summarize -> autofix attempt
    (reported, not asserted to succeed).

    Requires SENTRY_DSN + SENTRY_AUTH_TOKEN in the ambient env. Skips (not
    fails) without credentials — the no-fabrication gate.
    """
    ambient = credential_env_snapshot_and_clear
    if not (ambient.get("SENTRY_AUTH_TOKEN") and ambient.get("SENTRY_DSN")):
        pytest.skip("no Sentry credentials in ambient env (SENTRY_DSN / SENTRY_AUTH_TOKEN absent)")
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)

    token = os.environ["SENTRY_AUTH_TOKEN"]
    org = os.environ.get("SENTRY_ORG") or seer_module.SeerClient.DEFAULT_ORG
    project = os.environ.get("SENTRY_PROJECT") or seer_module.SeerClient.DEFAULT_PROJECT

    client = seer_module.SeerClient()  # reads env itself
    check = client.setup_check()
    assert check["ok"] is True, check
    assert "setupAcknowledgement" in check["body"]

    # Seer requires an existing issue; drive one from a FRESH metadata-only
    # event. Per-run unique ids + a non-null handoff_id make the envelope
    # schema-valid AND make every join below provably THIS run's ingestion.
    run_hex = uuid.uuid4().hex
    envelope = {
        "board": "sovav-hermes",
        "task_id": "t_e246539f",
        "run_id": 28,
        "parent_task_ids": [],
        "profile/role": "worker",
        "model": "z-ai/glm-5.3-flash",
        "provider": "openrouter",
        "key_id": "unlabeled",
        "call_class": "aux",
        "handoff_id": f"hnd-{run_hex[12:24]}",
        "worktree/branch": "wt/htb-b4b",
        "trace_id": f"trc-{run_hex[24:36]}",
        "repo": None,
    }
    event = {
        "event_id": f"ev-{run_hex[:12]}",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "attempt": 1,
        "envelope": envelope,
    }
    exporter = sinks_module.SentryExporter()  # reads SENTRY_DSN from env
    assert exporter.configured, "SENTRY_DSN not set in ambient env"
    delivered_after = datetime.now(timezone.utc)  # fresh-object lower bound, pre-delivery
    delivered = exporter.deliver([event])
    assert delivered["ok"] is True, delivered["detail"]

    # Prove fresh ingestion of THIS run's event (polls past async ingest
    # latency), and join Seer to the issue group the fresh event itself
    # landed in — not by title, which is shared across ALL telemetry events.
    sentry_event_id = _sentry_event_id_for(event["event_id"])
    event_body = _poll_sentry_event(token, org, project, sentry_event_id)
    assert event_body["eventID"] == sentry_event_id
    # API creation timestamp inside THIS run's delivery window (freshness).
    _assert_fresh_datecreated(event_body, delivered_after)
    tags = {t["key"]: t["value"] for t in event_body["tags"]}
    assert tags["event_id"] == event["event_id"]
    assert tags["trace_id"] == envelope["trace_id"]
    assert tags["handoff_id"] == envelope["handoff_id"]
    group_id = event_body["groupID"]

    analysis = client.summarize_issue(group_id, event_id=sentry_event_id)
    assert analysis["ok"] is True, analysis
    body = analysis["body"]
    for key in ("headline", "whatsWrong", "possibleCause"):
        assert key in body
    # The returned analysis must reference the FRESH event id — an analysis
    # of an older sibling event does not prove this run's loop. The forced
    # event is pinned via the endpoint's force_event_id (verified against
    # live getsentry source), so the response's eventId must be ours.
    serialized_analysis = json.dumps(body)
    assert sentry_event_id in serialized_analysis, serialized_analysis

    # §3.3 negative scan over the round-tripped analysis.
    for marker in MARKERS:
        assert marker not in serialized_analysis

    autofix = client.trigger_autofix(group_id)
    # Either the run starts (repos connected) or Sentry refuses with a coded
    # reason (sandbox: repos_not_linked). Both are real API outcomes; only a
    # transport-level failure would be a defect.
    assert autofix["status"] is not None
    status = client.autofix_status(group_id)
    assert status["ok"] is True
