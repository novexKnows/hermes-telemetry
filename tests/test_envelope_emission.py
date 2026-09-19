"""NEGATIVE redaction tests for correlation-envelope emission (HTB-B1).

For each capture point family (session / llm / api / tool / subagent) we feed
adversarial hook payloads containing prompt text, response text, fake secrets
(`sk-FAKE-...` marker strings) and file contents, then deep-scan the serialized
in-memory emission record (envelope + event body, and everything the module
persists — which is nothing: emission is in-memory only on this branch) and
assert every banned marker appears NOWHERE.

Positive checks in the same file: schema conformance of emitted envelopes,
metadata-only event bodies, kill switch, ring bound, harness-pair propagation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_init_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_init_mod)
sys.modules["hermes_telemetry"] = _init_mod

import hermes_telemetry.envelope as envelope  # noqa: E402
import hermes_telemetry.redaction as redaction  # noqa: E402

# Banned-content markers (fake values — never real secrets).
PROMPT_MARKER = "ADVERSARIAL-PROMPT-TEXT-7f3a1b"
RESPONSE_MARKER = "ADVERSARIAL-RESPONSE-TEXT-91c04d"
FAKE_KEY = "sk-FAKE-TEST-KEY-0123456789abcdef"
FAKE_KEY2 = "ghp_FAKEPATaaaaaaaaaaaaaaaaaaaaaa"
FILE_MARKER = "ADVERSARIAL-FILE-CONTENTS-44be21"
BANNED_MARKERS = (PROMPT_MARKER, RESPONSE_MARKER, FAKE_KEY, FAKE_KEY2, FILE_MARKER)


class MockPluginContext:
    def __init__(self, profile_name="worker"):
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


@pytest.fixture(autouse=True)
def clean_envelope_state(monkeypatch, tmp_path):
    """Hermetic env + fresh in-memory ring for every test.

    B1 isolation hardening (TM directive after the 2026-09-19 incident):
    every test here can trigger plugin persistence, so BOTH HERMES_HOME
    (pinned by conftest) and HERMES_TELEMETRY_HOME are explicitly overridden
    to the same disposable tmp home — an inherited ambient value must never
    route writes to the real telemetry DB.
    """
    for var in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_PARENT_TASK_IDS",
        "HERMES_TELEMETRY_KEY_ID",
        "HERMES_TELEMETRY_ENVELOPE_ENABLED",
        "TURNSTONE_HARNESS_TASK_ID",
        "TURNSTONE_HARNESS_RUN_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TELEMETRY_NO_SETUP", "1")
    envelope.clear_records()
    envelope.reset_derivation_cache()
    redaction.reset_stats()
    yield
    envelope.clear_records()


@pytest.fixture()
def ctx():
    c = MockPluginContext()
    _init_mod.register(c)
    return c


def _deep_scan() -> str:
    """Serialize EVERYTHING the emission layer produced in this test."""
    records = envelope.recent_records()
    return json.dumps(records) + repr(records)


def _assert_no_banned(scan_text: str):
    for marker in BANNED_MARKERS:
        assert marker not in scan_text, f"banned content leaked: {marker}"


# ---------------------------------------------------------------------------
# Session family: on_session_start / on_session_end / on_session_finalize
# ---------------------------------------------------------------------------


def test_neg_session_start_drops_user_message_and_history(ctx):
    ctx.fire(
        "on_session_start",
        session_id="sess_a",
        model="m1",
        platform="cli",
        user_message=PROMPT_MARKER,
        conversation_history=[{"role": "user", "content": RESPONSE_MARKER}],
        request_messages=[{"content": FILE_MARKER}],
    )
    _assert_no_banned(_deep_scan())


def test_neg_session_start_secret_shaped_model_scrubbed(ctx):
    ctx.fire("on_session_start", session_id="sess_a", model=FAKE_KEY, platform="cli")
    scan = _deep_scan()
    _assert_no_banned(scan)
    records = envelope.recent_records()
    assert len(records) == 1
    assert records[0]["envelope"]["model"] == redaction.REDACTED


def test_neg_session_end_drops_transcript_kwargs(ctx):
    ctx.fire(
        "on_session_end",
        session_id="sess_a",
        completed=False,
        interrupted=True,
        user_message=PROMPT_MARKER,
        transcript=FILE_MARKER,
        model=FAKE_KEY2,
        platform="cli",
    )
    _assert_no_banned(_deep_scan())


def test_neg_session_finalize_drops_adversarial_kwargs(ctx):
    ctx.fire(
        "on_session_finalize",
        session_id="sess_a",
        platform="cli",
        api_kwargs={"model": FAKE_KEY, "prompt": PROMPT_MARKER},
        error_message=RESPONSE_MARKER,
    )
    _assert_no_banned(_deep_scan())


# ---------------------------------------------------------------------------
# LLM family: pre_llm_call / post_llm_call
# ---------------------------------------------------------------------------


def test_neg_pre_llm_call_drops_user_message_and_history(ctx):
    ctx.fire(
        "pre_llm_call",
        session_id="sess_a",
        sender_id="user1",
        user_message=PROMPT_MARKER,
        conversation_history=[
            {"role": "assistant", "content": RESPONSE_MARKER},
            {"role": "user", "content": f"{FILE_MARKER} {FAKE_KEY}"},
        ],
        model="m1",
        platform="cli",
        is_first_turn=True,
    )
    _assert_no_banned(_deep_scan())


def test_neg_pre_llm_call_secret_shaped_sender_id_scrubbed(ctx):
    ctx.fire("pre_llm_call", session_id="sess_a", sender_id=FAKE_KEY, user_message=PROMPT_MARKER)
    scan = _deep_scan()
    _assert_no_banned(scan)
    records = envelope.recent_records()
    assert records[0]["event"]["sender_id"] == redaction.REDACTED
    # and the run's sender capture in the DB was already sanitized upstream —
    # the marker must not appear in the emitted record either way.


def test_neg_post_llm_call_drops_response_text(ctx):
    ctx.fire(
        "post_llm_call",
        session_id="sess_a",
        response_text=RESPONSE_MARKER,
        output_text=FILE_MARKER,
        prompt=PROMPT_MARKER,
        model="m1",
        platform="cli",
    )
    _assert_no_banned(_deep_scan())


# ---------------------------------------------------------------------------
# API family: pre_api_request / post_api_request / api_request_error
# ---------------------------------------------------------------------------


def test_neg_pre_api_request_drops_request_messages(ctx):
    ctx.fire(
        "pre_api_request",
        session_id="sess_a",
        api_call_count=0,
        approx_input_tokens=100,
        request_messages=[{"role": "user", "content": f"{PROMPT_MARKER} {FAKE_KEY} {FILE_MARKER}"}],
        model="m1",
        provider="p1",
    )
    _assert_no_banned(_deep_scan())


def test_neg_post_api_request_drops_payload_content_keeps_counters(ctx):
    ctx.fire(
        "post_api_request",
        session_id="sess_a",
        model="m1",
        provider="p1",
        api_duration=1.25,
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "leaked_prompt": PROMPT_MARKER,
            "leaked_response": RESPONSE_MARKER,
        },
        response_model="m1",
        api_call_count=0,
        assistant_content_chars=40,
        assistant_text=RESPONSE_MARKER,
        request_messages=[{"content": FILE_MARKER}],
        platform="cli",
    )
    _assert_no_banned(_deep_scan())
    event = envelope.recent_records()[-1]["event"]
    assert event["tokens_in"] == 10
    assert event["tokens_out"] == 5


def test_neg_post_api_request_secret_shaped_response_model_scrubbed(ctx):
    ctx.fire(
        "post_api_request",
        session_id="sess_a",
        model=FAKE_KEY,
        provider="p1",
        api_duration=0.5,
        usage=None,
        response_model=FAKE_KEY2,
        api_call_count=0,
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    env = envelope.recent_records()[-1]["envelope"]
    assert env["model"] == redaction.REDACTED


def test_neg_api_request_error_drops_error_message_and_api_kwargs(ctx):
    ctx.fire(
        "api_request_error",
        session_id="sess_a",
        api_kwargs={"model": FAKE_KEY, "messages": [{"content": PROMPT_MARKER}]},
        error_type="timeout",
        error_message=f"{RESPONSE_MARKER} {FILE_MARKER}",
        status_code=500,
        retryable=True,
        model=FAKE_KEY,
        provider="p1",
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    event = envelope.recent_records()[-1]["event"]
    assert event["status_code"] == 500
    assert event["error_type"] == "timeout"


# ---------------------------------------------------------------------------
# Tool family: pre_tool_call / post_tool_call
# ---------------------------------------------------------------------------


def test_neg_post_tool_call_json_result_dropped(ctx):
    ctx.fire(
        "post_tool_call",
        tool_name="read_file",
        result=json.dumps({"content": FILE_MARKER, "api_key": FAKE_KEY, "prompt": PROMPT_MARKER}),
        duration_ms=12,
        session_id="sess_a",
    )
    _assert_no_banned(_deep_scan())


def test_neg_post_tool_call_plain_text_result_dropped(ctx):
    ctx.fire(
        "post_tool_call",
        tool_name="run_cmd",
        result=f"stdout: {RESPONSE_MARKER}\nsecret: {FAKE_KEY2}\nfile: {FILE_MARKER}",
        duration_ms=5,
        session_id="sess_a",
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    event = envelope.recent_records()[-1]["event"]
    assert event["tool_name"] == "run_cmd"
    assert event["ok"] is True


def test_neg_pre_tool_call_args_dropped(ctx):
    ctx.fire(
        "pre_tool_call",
        session_id="sess_a",
        tool_name="write_file",
        args={"path": "C:/x.env", "content": f"{FILE_MARKER} {FAKE_KEY} {PROMPT_MARKER}"},
        tool_call_id="tc_1",
    )
    _assert_no_banned(_deep_scan())
    event = envelope.recent_records()[-1]["event"]
    assert event["tool_name"] == "write_file"


# ---------------------------------------------------------------------------
# Subagent family: subagent_start / subagent_stop
# ---------------------------------------------------------------------------


def test_neg_subagent_start_drops_child_goal(ctx):
    ctx.fire(
        "subagent_start",
        parent_session_id="sess_a",
        parent_turn_id="t1",
        parent_subagent_id="sa-1-aaaaaaaa",
        child_session_id="sess_b",
        child_subagent_id="sa-1-bbbbbbbb",
        child_role="worker",
        child_goal=f"{PROMPT_MARKER} {FAKE_KEY}",
    )
    _assert_no_banned(_deep_scan())


def test_neg_subagent_start_secret_shaped_subagent_id_scrubbed(ctx):
    ctx.fire(
        "subagent_start",
        parent_session_id="sess_a",
        child_session_id="sess_b",
        parent_subagent_id=FAKE_KEY2,
        child_subagent_id=FAKE_KEY,
        child_role="worker",
        child_goal=FILE_MARKER,
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    event = envelope.recent_records()[-1]["event"]
    assert event["parent_subagent_id"] == redaction.REDACTED
    assert event["child_subagent_id"] == redaction.REDACTED


def test_neg_subagent_stop_drops_child_summary(ctx):
    ctx.fire(
        "subagent_stop",
        parent_session_id="sess_a",
        child_session_id="sess_b",
        child_role="worker",
        child_summary=f"{RESPONSE_MARKER} {FILE_MARKER} {FAKE_KEY}",
        child_status="completed",
        duration_ms=42,
    )
    _assert_no_banned(_deep_scan())
    event = envelope.recent_records()[-1]["event"]
    assert event["child_status"] == "completed"


# ---------------------------------------------------------------------------
# v0.21.3 runtime shapes (A2 matrix G6/G7/G2 reconciliation)
# ---------------------------------------------------------------------------


def test_neg_post_tool_call_error_message_is_never_emitted_G6(ctx):
    """G6: v0.21.3 derives status/error_type/error_message for post_tool_call.
    status/error_type are metadata and ARE emitted; error_message is
    content-bearing prose and must appear NOWHERE (TM directive)."""
    ctx.fire(
        "post_tool_call",
        tool_name="write_file",
        args={"path": "C:/x"},
        result="rejected: file protected",
        duration_ms=9,
        session_id="sess_a",
        status="error",
        error_type="approval_denied",
        error_message=f"{PROMPT_MARKER} {FAKE_KEY} {FILE_MARKER}",
        middleware_trace=[{"span": RESPONSE_MARKER}],
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    event = envelope.recent_records()[-1]["event"]
    assert event["status"] == "error"
    assert event["error_type"] == "approval_denied"
    assert "error_message" not in event


def test_neg_api_request_error_v0213_nested_shape_G7(ctx):
    """G7: v0.21.3 passes error={"type","message"} and request={payload} —
    NOT the legacy flat api_kwargs/error_type/error_message. The error class
    must be extracted from error['type']; error['message'] and the whole
    request{} payload (which contains the outgoing messages) must leak
    NOWHERE."""
    ctx.fire(
        "api_request_error",
        session_id="sess_a",
        platform="cli",
        model="m1",
        provider="p1",
        api_call_count=0,
        status_code=503,
        retry_count=1,
        max_retries=3,
        retryable=True,
        reason="provider_error",
        error={"type": "provider_error", "message": f"{RESPONSE_MARKER} {FAKE_KEY}"},
        request={
            "messages": [{"role": "user", "content": f"{PROMPT_MARKER} {FILE_MARKER}"}],
            "model": FAKE_KEY2,
        },
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    event = envelope.recent_records()[-1]["event"]
    assert event["status_code"] == 503
    assert event["error_type"] == "provider_error"
    assert event["retry_count"] == 1


def test_neg_api_request_error_legacy_flat_shape_still_scanned(ctx):
    """Older-runtime flat shape (error_type/error_message top-level kwargs):
    the prose message is still never emitted."""
    ctx.fire(
        "api_request_error",
        session_id="sess_a",
        error_type="timeout",
        error_message=f"{PROMPT_MARKER} {FILE_MARKER}",
        api_kwargs={"model": FAKE_KEY, "input": RESPONSE_MARKER},
        status_code=500,
        retryable=False,
        model=FAKE_KEY,
        provider="p1",
    )
    _assert_no_banned(_deep_scan())


def test_moa_reference_count_emitted_not_contents_G2(ctx):
    """G2: v0.21.3 delivers per-advisor MoA metrics on moa_references. The
    envelope records only the reference COUNT; per-advisor model/provider
    names are metadata (emitted by B2's metrics.py, not duplicated here) and
    the metrics payloads never leak through emission."""
    refs = [
        {"provider": "openrouter", "model": "ref-a", "tokens": 100},
        {"provider": "openrouter", "model": "ref-b", "tokens": 200, "notes": FILE_MARKER},
    ]
    ctx.fire(
        "post_api_request",
        session_id="sess_a",
        model="moa-preset",
        provider="moa",
        api_duration=1.0,
        usage={"input_tokens": 10, "output_tokens": 5},
        api_call_count=0,
        moa_references=refs,
        platform="cli",
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    event = envelope.recent_records()[-1]["event"]
    assert event["moa_reference_count"] == 2


def test_isolation_telemetry_home_pinned_to_tmp():
    """Isolation regression guard (incident 2026-09-19): plugin persistence
    paths must resolve inside the test's disposable home, never the real
    home — HERMES_TELEMETRY_HOME outranks HERMES_HOME (paths.py:31), so the
    B1 fixture pins BOTH to the same tmp dir."""
    import hermes_telemetry.paths as paths

    home = Path(os.environ["HERMES_HOME"])
    telemetry_home = paths.get_telemetry_home()
    assert os.environ["HERMES_TELEMETRY_HOME"] == str(home)
    assert str(telemetry_home).startswith(str(home))


def test_isolation_real_home_env_overridden_by_conftest():
    """An inherited ambient HERMES_TELEMETRY_HOME pointing at the real
    telemetry DB must never survive into a B1 test: the conftest neutralizes
    it repo-wide and the B1 fixture re-pins it to the disposable tmp home."""
    real_env = os.environ.get("HERMES_TELEMETRY_HOME", "")
    tmp_home = os.environ["HERMES_HOME"]
    assert not real_env or real_env == tmp_home


# ---------------------------------------------------------------------------
# Positive conformance: schema-validity + metadata-only shape of emissions
# ---------------------------------------------------------------------------


def test_every_emitted_envelope_validates_against_pinned_schema(ctx):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (_ROOT / "schemas" / "correlation-envelope.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    ctx.fire("on_session_start", session_id="s", model="m", platform="cron")
    ctx.fire("pre_api_request", session_id="s", api_call_count=0, approx_input_tokens=1)
    ctx.fire(
        "post_api_request",
        session_id="s",
        model="m",
        provider="p",
        api_duration=0.1,
        usage={"input_tokens": 1, "output_tokens": 1},
        api_call_count=0,
        platform="cron",
    )
    ctx.fire("post_tool_call", tool_name="t", result="ok", duration_ms=1, session_id="s")
    ctx.fire(
        "subagent_stop",
        parent_session_id="s",
        child_session_id="c",
        child_status="completed",
        duration_ms=1,
    )
    records = envelope.recent_records()
    assert len(records) == 5
    for record in records:
        assert list(validator.iter_errors(record["envelope"])) == []
        assert envelope.structural_errors(record["envelope"]) == []


def test_emitted_event_bodies_are_metadata_only(ctx):
    all_hooks = [
        "on_session_start",
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "post_tool_call",
        "post_llm_call",
        "on_session_end",
        "on_session_finalize",
        "subagent_start",
        "subagent_stop",
        "pre_llm_call",
        "pre_tool_call",
    ]
    for hook in all_hooks:
        ctx.fire(hook, session_id="s", model="m", provider="p", platform="cli")
    records = envelope.recent_records()
    assert len(records) == len(all_hooks)
    for record in records:
        for key in record["event"]:
            assert key in redaction.ALLOWED_EVENT_KEYS, f"non-allowlisted key: {key}"
        # every emitted hook is one of the plugin's registered capture points
        assert record["event"]["hook"] in all_hooks


def test_call_class_derivation(ctx):
    ctx.fire("on_session_start", session_id="s", model="m", platform="cron")
    ctx.fire("post_tool_call", tool_name="t", result="ok", duration_ms=1, session_id="s")
    ctx.fire("subagent_start", parent_session_id="s", child_session_id="c", child_role="worker")
    records = envelope.recent_records()
    assert records[0]["envelope"]["call_class"] == "cron"
    assert records[1]["envelope"]["call_class"] == "primary"
    assert records[2]["envelope"]["call_class"] == "delegate"


def test_kill_switch_disables_emission(ctx, monkeypatch):
    monkeypatch.setenv("HERMES_TELEMETRY_ENVELOPE_ENABLED", "0")
    ctx.fire("on_session_start", session_id="s", model="m", platform="cli")
    ctx.fire("post_tool_call", tool_name="t", result="ok", duration_ms=1, session_id="s")
    assert envelope.recent_records() == []


def test_ring_is_bounded(ctx):
    for i in range(1100):
        ctx.fire("post_llm_call", session_id=f"s{i}", model="m", platform="cli")
    assert len(envelope.recent_records()) == envelope._BUFFER_SIZE


def test_emission_persists_nothing(ctx, tmp_path):
    """The emission layer is in-memory only on this branch: no envelope file
    is ever created under the (isolated) HERMES_HOME telemetry dir."""
    hermes_home = Path(os.environ["HERMES_HOME"])
    before = {p.name for p in hermes_home.rglob("*")}
    ctx.fire("on_session_start", session_id="s", model="m", platform="cli")
    ctx.fire(
        "post_api_request",
        session_id="s",
        model="m",
        provider="p",
        api_duration=0.1,
        usage={"input_tokens": 1, "output_tokens": 1},
        api_call_count=0,
    )
    after = {p.name for p in hermes_home.rglob("*")}
    new_files = after - before
    assert not any("envelope" in n.lower() or n.endswith(".jsonl") for n in new_files), new_files


def test_harness_pair_propagation_from_env(ctx, monkeypatch):
    monkeypatch.setenv("TURNSTONE_HARNESS_TASK_ID", "t_parent")
    monkeypatch.setenv("TURNSTONE_HARNESS_RUN_ID", "55")
    ctx.fire("on_session_start", session_id="s", model="m", platform="cli")
    env = envelope.recent_records()[-1]["envelope"]
    assert env["harness_task_id"] == "t_parent"
    assert env["harness_run_id"] == "55"


def test_harness_pair_stays_null_when_half_populated(ctx, monkeypatch):
    monkeypatch.setenv("TURNSTONE_HARNESS_TASK_ID", "t_parent")
    ctx.fire("on_session_start", session_id="s", model="m", platform="cli")
    env = envelope.recent_records()[-1]["envelope"]
    assert env["harness_task_id"] is None
    assert env["harness_run_id"] is None


def test_kanban_env_vars_populate_board_task_run(ctx, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "hermes-telemetry")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_3af9b6ed")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "6")
    monkeypatch.setenv("HERMES_KANBAN_PARENT_TASK_IDS", "t_81261ac8, t_9b7cac11")
    ctx.fire("on_session_start", session_id="s", model="m", platform="cli")
    env = envelope.recent_records()[-1]["envelope"]
    assert env["board"] == "hermes-telemetry"
    assert env["task_id"] == "t_3af9b6ed"
    assert env["run_id"] == 6 or env["run_id"] == "6"
    assert env["parent_task_ids"] == ["t_81261ac8", "t_9b7cac11"]


def test_trace_id_is_deterministic_per_session_and_stable(ctx):
    ctx.fire("on_session_start", session_id="sess_same", model="m", platform="cli")
    ctx.fire("on_session_end", session_id="sess_same", completed=True)
    records = envelope.recent_records()
    assert records[0]["envelope"]["trace_id"] == records[1]["envelope"]["trace_id"]
    assert records[0]["envelope"]["trace_id"].startswith("trc-")


def test_budget_path_unaffected_by_emission(ctx):
    """Emission must never sit on the budget critical path: a hard breach is
    still blocked exactly as before, with emission merely observing."""
    import hermes_telemetry.db as db

    ctx.fire("on_session_start", session_id="s", model="m", platform="cli")
    ctx.fire("pre_tool_call", session_id="s", tool_name="t", args={})
    # No budget configured → no block, and emission returned a record anyway.
    assert ctx.hooks["pre_tool_call"](session_id="s") is None
    # on_session_start + the two pre_tool_call invocations, each observed
    # without perturbing the budget verdict (None returned, no block dict).
    assert len(envelope.recent_records()) == 3
    assert db.get_run("s") is not None


# ---------------------------------------------------------------------------
# Unicode default-ignorable obfuscation (HTB-B1-FIX). A U+200B inserted
# after "sk-" previously defeated the value scrubber and let the exact
# secret value ride on allowlisted envelope.model and event.model. These
# tests reproduce the reviewer probe class across envelope AND event
# allowlisted fields (U+200B + at least one other default-ignorable
# variant), plus a nested unknown-kwarg case, deep-scanning every emission
# (json.dumps + repr) the way the negative tests above do.
# ---------------------------------------------------------------------------

OBF_SECRET_BODY = "FAKEKEY0123456789abcdef"
OBF_SEPARATORS = (
    "\u200b",  # ZERO WIDTH SPACE (Cf) — the reviewer probe variant
    "\u2060",  # WORD JOINER (Cf)
    "\u00ad",  # SOFT HYPHEN (Cf)
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM (Cf)
)


def _assert_no_obfuscation(scan_text: str):
    for sep in OBF_SEPARATORS:
        assert sep not in scan_text, f"default-ignorable U+{ord(sep):04X} survived emission"


def test_neg_session_start_zwsp_obfuscated_model_scrubbed_envelope_and_event(ctx):
    """U+200B after the sk- prefix must not let the secret reach the
    allowlisted envelope.model or event.model fields."""
    ctx.fire(
        "on_session_start",
        session_id="sess_a",
        model=f"sk-\u200b{OBF_SECRET_BODY}",
        platform="cli",
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    _assert_no_obfuscation(scan)
    assert OBF_SECRET_BODY not in scan
    record = envelope.recent_records()[-1]
    assert record["envelope"]["model"] == redaction.REDACTED
    assert record["event"]["model"] == redaction.REDACTED


def test_neg_post_llm_call_soft_hyphen_obfuscated_model_scrubbed(ctx):
    """Second default-ignorable variant (U+00AD) through a different capture
    point, hitting both the envelope and the event allowlisted model field."""
    ctx.fire(
        "post_llm_call",
        session_id="sess_a",
        model=f"sk-\u00ad{OBF_SECRET_BODY}",
        platform="cli",
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    _assert_no_obfuscation(scan)
    assert OBF_SECRET_BODY not in scan
    record = envelope.recent_records()[-1]
    assert record["envelope"]["model"] == redaction.REDACTED
    assert record["event"]["model"] == redaction.REDACTED


def test_neg_post_tool_call_word_joiner_obfuscated_tool_name_scrubbed(ctx):
    """Obfuscated secret-shape riding an event allowlisted field other than
    model (tool_name) is scrubbed too."""
    ctx.fire(
        "post_tool_call",
        tool_name=f"sk-\u2060{OBF_SECRET_BODY}",
        result="ok",
        duration_ms=1,
        session_id="sess_a",
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    _assert_no_obfuscation(scan)
    assert OBF_SECRET_BODY not in scan
    record = envelope.recent_records()[-1]
    assert record["event"]["tool_name"] == redaction.REDACTED


def test_neg_post_api_request_bom_obfuscated_provider_scrubbed(ctx):
    """Envelope.provider (allowlisted) with a U+FEFF obfuscation variant —
    an obfuscated SECRET SHAPE riding the provider field is scrubbed."""
    ctx.fire(
        "post_api_request",
        session_id="sess_a",
        model="m1",
        provider=f"\ufeffsk-\ufeff{OBF_SECRET_BODY}",
        api_duration=0.1,
        usage={"input_tokens": 1, "output_tokens": 1},
        api_call_count=0,
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    _assert_no_obfuscation(scan)
    assert OBF_SECRET_BODY not in scan
    record = envelope.recent_records()[-1]
    assert record["envelope"]["provider"] == redaction.REDACTED
    assert record["event"]["provider"] == redaction.REDACTED


def test_neg_nested_unknown_kwarg_obfuscated_secret_dropped(ctx):
    """A secret-shaped value hidden in a NESTED unknown kwarg is dropped by
    the allowlist regardless of obfuscation — and even in the dropped case
    nothing ignorable or secret-shaped can appear in any emission."""
    ctx.fire(
        "on_session_finalize",
        session_id="sess_a",
        platform="cli",
        api_kwargs={
            "model": f"sk-\u200b{OBF_SECRET_BODY}",
            "prompt": f"{PROMPT_MARKER} \u2060{OBF_SECRET_BODY}",
        },
    )
    scan = _deep_scan()
    _assert_no_banned(scan)
    _assert_no_obfuscation(scan)
    assert OBF_SECRET_BODY not in scan
    for record in envelope.recent_records():
        assert "api_kwargs" not in record["event"]
