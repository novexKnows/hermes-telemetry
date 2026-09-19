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

import pytest

import seer as seer_module
import sinks as sinks_module

SENTRY_CREDENTIAL_ENV_VARS = ["SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT"]

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


def test_integration_seer_sandbox_analysis_loop(credential_env_snapshot_and_clear, monkeypatch):
    """Real Seer loop: setup-check -> ingest metadata-only event -> find its
    issue -> summarize -> autofix attempt (reported, not asserted to succeed).

    Requires SENTRY_AUTH_TOKEN in the ambient env. Skips (not fails) without
    credentials — the no-fabrication gate.
    """
    ambient = credential_env_snapshot_and_clear
    if not ambient.get("SENTRY_AUTH_TOKEN"):
        pytest.skip("no Sentry credentials in ambient env (SENTRY_AUTH_TOKEN absent)")
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)

    from urllib import request as urlreq

    token = os.environ["SENTRY_AUTH_TOKEN"]
    org = os.environ.get("SENTRY_ORG") or seer_module.SeerClient.DEFAULT_ORG
    project = os.environ.get("SENTRY_PROJECT") or seer_module.SeerClient.DEFAULT_PROJECT
    api = "https://sentry.io/api/0"

    client = seer_module.SeerClient()  # reads env itself
    check = client.setup_check()
    assert check["ok"] is True, check
    assert "setupAcknowledgement" in check["body"]

    # Seer requires an existing issue; drive one from a fresh metadata-only
    # event delivered through the verified ingestion path, then find its group.
    envelope = {
        "board": "sovav-hermes",
        "task_id": "t_e3985496",
        "run_id": 22,
        "parent_task_ids": [],
        "profile/role": "worker",
        "model": "z-ai/glm-5.3-flash",
        "provider": "openrouter",
        "key_id": "unlabeled",
        "call_class": "aux",
        "handoff_id": None,
        "worktree/branch": "wt/htb-b4b",
        "trace_id": "trc_htb4b_seer_0001",
        "repo": None,
    }
    event = {
        "event_id": "ev-9201",
        "enqueued_at": "2026-09-19T16:30:00.000000+00:00",
        "attempt": 1,
        "envelope": envelope,
    }
    exporter = sinks_module.SentryExporter()  # reads SENTRY_DSN from env
    assert exporter.configured, "SENTRY_DSN not set in ambient env"
    delivered = exporter.deliver([event])
    assert delivered["ok"] is True, delivered["detail"]

    req = urlreq.Request(
        f"{api}/projects/{org}/{project}/issues/?query=&per_page=10",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlreq.urlopen(req, timeout=15) as resp:
        issues = json.load(resp)
    our_issue = next(
        (i for i in issues if i.get("title") == sinks_module.SentryExporter.RUN_MESSAGE), None
    )
    assert our_issue is not None, "telemetry issue group not found in project"

    analysis = client.summarize_issue(our_issue["id"])
    assert analysis["ok"] is True, analysis
    body = analysis["body"]
    for key in ("headline", "whatsWrong", "possibleCause"):
        assert key in body
    # §3.3 negative scan over the round-tripped analysis.
    serialized = json.dumps(body)
    for marker in MARKERS:
        assert marker not in serialized

    autofix = client.trigger_autofix(our_issue["id"])
    # Either the run starts (repos connected) or Sentry refuses with a coded
    # reason (sandbox: repos_not_linked). Both are real API outcomes; only a
    # transport-level failure would be a defect.
    assert autofix["status"] is not None
    status = client.autofix_status(our_issue["id"])
    assert status["ok"] is True
