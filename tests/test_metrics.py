"""tests/test_metrics.py — HTB-B2 utilization-metrics extraction.

Fixtures below are synthesized from the INSTALLED v0.21.3 hook fire sites
(verified kwarg-by-kwarg against C:/Users/Sovav/AppData/Local/hermes/hermes-agent
at commit 0ddba07ad7; see docs/telemetry/utilization-metrics-mapping.md for the
file:line evidence). Pure functions — no DB, no HERMES_HOME state.
"""

# conftest.py aliases the repo root as the `hermes_telemetry` package.
import hermes_telemetry.metrics as metrics  # noqa: E402
import pytest

# ---------------------------------------------------------------------------
# Fixtures — kwargs exactly as the v0.21.3 fire sites pass them
# ---------------------------------------------------------------------------

# agent/turn_response_intake.py:62-91
POST_API_REQUEST_KWARGS = {
    "task_id": "0f2f1e5a-1111-4c33-9c33-3f9f04686ba1",
    "turn_id": "turn_9f2c41",
    "api_request_id": "req_8891",
    "session_id": "cron_my_job_2_20260919_101500",
    "platform": "cron",
    "model": "z-ai/glm-5.3-flash",
    "provider": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "api_mode": "chat",
    "api_call_count": 2,
    "api_duration": 1.2345,
    "started_at": 1000.0,
    "ended_at": 1001.2345,
    "first_chunk_at": 1000.25,
    "finish_reason": "tool_calls",
    "message_count": 12,
    "response_model": "z-ai/glm-5.3-flash",
    "response": {
        "id": "gen-01ABCDEF",
        "provider": "Z-AI",
        "billed_cost_usd": 0.0042,
        "model": "z-ai/glm-5.3-flash",
        "finish_reason": "tool_calls",
        "assistant_message": {"role": "assistant", "content": "", "tool_calls": []},
        "usage": None,
    },
    "usage": {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 30,
        "cache_write_tokens": 20,
        "reasoning_tokens": 10,
        "request_count": 1,
        "prompt_tokens": 150,  # = 100 + 30 + 20
        "total_tokens": 200,  # = 150 + 50
    },
    "assistant_message": "…",
    "assistant_content_chars": 432,
    "assistant_tool_call_count": 1,
    "moa_references": None,
}

# agent/api_request_hooks.py:175-212
API_REQUEST_ERROR_KWARGS = {
    "task_id": "0f2f1e5a-1111-4c33-9c33-3f9f04686ba1",
    "turn_id": "turn_9f2c41",
    "api_request_id": "req_8892",
    "session_id": "cron_my_job_2_20260919_101500",
    "platform": "cron",
    "model": "z-ai/glm-5.3-flash",
    "provider": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "api_mode": "chat",
    "api_call_count": 3,
    "api_duration": 0.5,
    "started_at": 1002.0,
    "ended_at": 1002.5,
    "status_code": 404,
    "retry_count": 0,
    "max_retries": 2,
    "retryable": False,
    "reason": "model_not_found",
    "error": {"type": "NotFoundError", "message": "404 model removed"},
    "request": {"method": "POST", "body": {}},
}

# hermes_cli/kanban_db.py:157 + 165-172 + 2288
KANBAN_CLAIMED_KWARGS = {
    "task_id": "t_77d64af1",
    "profile_name": "worker",
    "board": "hermes-telemetry",
    "assignee": "worker",
    "run_id": 7,
}

# hermes_cli/kanban_db_dispatch.py:1156-1164 (+ board/profile via kanban_db.py:157)
KANBAN_WORKER_EXITED_KWARGS = {
    "task_id": "t_77d64af1",
    "profile_name": "worker",
    "board": "hermes-telemetry",
    "assignee": "worker",
    "run_id": 7,
    "worker_pid": 1900,
    "exit_kind": "nonzero_exit",
    "exit_code": 1,
    "outcome": "failed",
    "retry_status": "retrying",
}


# ---------------------------------------------------------------------------
# Envelope attribution from kanban lifecycle hooks
# ---------------------------------------------------------------------------


class TestEnvelopeAttribution:
    def test_claimed_maps_schema_field_names_and_types(self):
        out = metrics.envelope_attribution_from_kanban_claimed(KANBAN_CLAIMED_KWARGS)
        assert out["board"] == "hermes-telemetry"
        assert out["task_id"] == "t_77d64af1"
        assert out["run_id"] == 7 and isinstance(out["run_id"], int)
        assert out["profile/role"] == "worker"  # slash is part of the canonical name
        assert out["assignee"] == "worker"

    def test_completed_adds_summary(self):
        out = metrics.envelope_attribution_from_kanban_completed(
            {**KANBAN_CLAIMED_KWARGS, "summary": "implemented metrics extraction"}
        )
        assert out["summary"] == "implemented metrics extraction"
        assert out["task_id"] == "t_77d64af1"

    def test_blocked_adds_reason_and_missing_fields_are_dropped(self):
        out = metrics.envelope_attribution_from_kanban_blocked(
            {"task_id": "t_x", "run_id": None, "reason": "needs_input: waiting on creds"}
        )
        assert out["reason"] == "needs_input: waiting on creds"
        assert "run_id" not in out  # None dropped, not coerced to 0
        assert "board" not in out

    def test_run_id_string_is_coerced_to_int(self):
        out = metrics.envelope_attribution_from_kanban_claimed({"task_id": "t_x", "run_id": "42"})
        assert out["run_id"] == 42


# ---------------------------------------------------------------------------
# cron job id extraction (cron/scheduler.py:2302 format)
# ---------------------------------------------------------------------------


class TestCronJobId:
    @pytest.mark.parametrize(
        ("session_id", "expected"),
        [
            ("cron_my_job_2_20260919_101500", "my_job_2"),  # underscores inside job id
            ("cron_nightly_20260919_101500", "nightly"),
            ("cron_a_20260101_000000", "a"),
        ],
    )
    def test_parses_job_id(self, session_id, expected):
        assert metrics.cron_job_id_from_session_id(session_id) == expected

    @pytest.mark.parametrize(
        "session_id",
        ["20260919_101500_abc123", "", "cron_", "cronjob_20260101_000000", "cron_x_short"],
    )
    def test_non_cron_or_malformed_returns_none(self, session_id):
        assert metrics.cron_job_id_from_session_id(session_id) is None


# ---------------------------------------------------------------------------
# call_class — only cron / delegate are evidenceable; never guess the rest
# ---------------------------------------------------------------------------


class TestCallClass:
    def test_cron_platform(self):
        assert metrics.call_class_from_session_context(platform="cron") == "cron"

    def test_cron_session_prefix(self):
        assert (
            metrics.call_class_from_session_context(session_id="cron_j_20260101_000000") == "cron"
        )

    def test_delegate_via_parent_session(self):
        assert (
            metrics.call_class_from_session_context(
                platform="cli", parent_session_id="20260919_101500_abc123"
            )
            == "delegate"
        )

    def test_interactive_is_none_not_primary(self):
        # "primary" is a caller-level default; extraction does not assert it.
        assert metrics.call_class_from_session_context(platform="cli") is None

    def test_never_emits_unsourceable_classes(self):
        for platform in ("telegram", "discord", "gateway", ""):
            assert metrics.call_class_from_session_context(platform=platform) is None
        # The schema enum itself is intact; extraction just never *infers* these.
        assert set(metrics.CALL_CLASS_VALUES) == {
            "primary",
            "fallback",
            "aux",
            "delegate",
            "judge",
            "cron",
        }


# ---------------------------------------------------------------------------
# LLM call context
# ---------------------------------------------------------------------------


class TestCallContext:
    def test_pre_llm_call_context(self):
        # agent/turn_context.py:693-706
        out = metrics.call_context_from_pre_llm_call(
            {
                "session_id": "s1",
                "task_id": "uuid-turn",
                "turn_id": "turn_1",
                "user_message": "hi",
                "conversation_history": [],
                "is_first_turn": True,
                "model": "z-ai/glm-5.3-flash",
                "platform": "cli",
                "parent_session_id": "",
                "sender_id": "user-42",
            }
        )
        assert out["model"] == "z-ai/glm-5.3-flash"
        assert out["sender_id"] == "user-42"
        assert out["is_first_turn"] is True
        assert out.get("call_class") is None  # empty parent, non-cron platform
        assert "provider" not in out  # pre_llm_call carries no provider

    def test_post_api_request_context(self):
        out = metrics.call_context_from_post_api_request(POST_API_REQUEST_KWARGS)
        assert out["provider"] == "openrouter"
        assert out["api_call_count"] == 2
        assert out["api_request_id"] == "req_8891"
        assert out["response_model"] == "z-ai/glm-5.3-flash"


# ---------------------------------------------------------------------------
# Usage + billing (post_api_request)
# ---------------------------------------------------------------------------


class TestUsageMetrics:
    def test_all_token_buckets_extracted(self):
        out = metrics.usage_metrics_from_post_api_request(POST_API_REQUEST_KWARGS)
        assert out["input_tokens"] == 100
        assert out["output_tokens"] == 50
        assert out["reasoning_tokens"] == 10
        assert out["cache_read_tokens"] == 30
        assert out["cache_write_tokens"] == 20
        assert out["prompt_tokens"] == 150
        assert out["total_tokens"] == 200
        assert out["request_count"] == 1

    def test_latency_seconds_converted_to_ms_and_ttfb(self):
        out = metrics.usage_metrics_from_post_api_request(POST_API_REQUEST_KWARGS)
        assert out["api_duration_ms"] == 1234  # 1.2345 s → ms (round-half-even)
        assert out["api_started_at"] == 1000.0
        assert out["api_ended_at"] == 1001.2345
        assert out["time_to_first_token_s"] == pytest.approx(0.25)

    def test_usage_none_emits_no_token_keys(self):
        kwargs = dict(POST_API_REQUEST_KWARGS, usage=None)
        out = metrics.usage_metrics_from_post_api_request(kwargs)
        assert "input_tokens" not in out
        assert "total_tokens" not in out
        # latency is independent of usage and still extracted
        assert out["api_duration_ms"] == 1234

    def test_usage_garbage_values_ignored(self):
        kwargs = dict(POST_API_REQUEST_KWARGS, usage={"input_tokens": "not-a-number"})
        out = metrics.usage_metrics_from_post_api_request(kwargs)
        assert "input_tokens" not in out


class TestBillingMetrics:
    def test_provider_reported_cost_and_serving_provider(self):
        out = metrics.billing_metrics_from_post_api_request(POST_API_REQUEST_KWARGS)
        assert out["billed_cost_usd"] == pytest.approx(0.0042)
        assert out["serving_provider"] == "Z-AI"
        assert out["provider_response_id"] == "gen-01ABCDEF"

    def test_absent_response_emits_nothing(self):
        assert metrics.billing_metrics_from_post_api_request({"response": None}) == {}
        assert metrics.billing_metrics_from_post_api_request({}) == {}

    def test_moa_references_passed_through_when_present(self):
        refs = [{"role": "advisor", "model": "m1"}]
        out = metrics.billing_metrics_from_post_api_request(
            {"response": {}, "moa_references": refs}
        )
        assert out["moa_references"] == refs


# ---------------------------------------------------------------------------
# Tool calls, API errors, session outcome
# ---------------------------------------------------------------------------


class TestToolAndErrorMetrics:
    def test_post_tool_call_extraction(self):
        # model_tools.py:676-695 (+ _CallIds 643-653, None → "")
        out = metrics.tool_call_metrics_from_post_tool_call(
            {
                "tool_name": "terminal",
                "args": {"command": "pytest -q"},
                "result": "ok",
                "task_id": "uuid-turn",
                "session_id": "s1",
                "tool_call_id": "call_01",
                "turn_id": "turn_1",
                "api_request_id": "req_1",
                "duration_ms": 250,
                "status": "ok",
                "error_type": None,
                "error_message": None,
                "middleware_trace": [],
            }
        )
        assert out["tool_name"] == "terminal"
        assert out["tool_call_id"] == "call_01"
        assert out["tool_duration_ms"] == 250
        assert out["tool_status"] == "ok"
        assert "error_type" not in out

    def test_api_request_error_extraction(self):
        out = metrics.api_error_metrics_from_api_request_error(API_REQUEST_ERROR_KWARGS)
        assert out["status_code"] == 404
        assert out["retryable"] is False
        assert out["reason"] == "model_not_found"
        assert out["error_type"] == "NotFoundError"
        assert out["max_retries"] == 2
        assert out["api_duration_ms"] == 500

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"completed": True, "interrupted": False}, "ok"),
            ({"completed": False, "interrupted": True}, "interrupted"),
            ({"completed": False, "interrupted": False}, "error"),
            ({"completed": True, "interrupted": True}, "interrupted"),
            ({}, "error"),
        ],
    )
    def test_session_outcome_statuses(self, kwargs, expected):
        assert metrics.session_outcome_from_on_session_end(kwargs)["run_status"] == expected

    def test_session_outcome_carries_exit_reason(self):
        out = metrics.session_outcome_from_on_session_end(
            {"completed": False, "interrupted": True, "turn_exit_reason": "user_stop"}
        )
        assert out["turn_exit_reason"] == "user_stop"


# ---------------------------------------------------------------------------
# Delegation + kanban dispatch observers
# ---------------------------------------------------------------------------


class TestDelegationMetrics:
    def test_subagent_start_edge(self):
        # tools/delegate_tool.py:287-295
        out = metrics.delegation_metrics_from_subagent_start(
            {
                "parent_session_id": "p1",
                "parent_turn_id": "turn_1",
                "parent_subagent_id": "sa-0-root",
                "child_session_id": "c1",
                "child_subagent_id": "sa-0-ab12cd34",
                "child_role": "worker",
                "child_goal": "do the thing",
            }
        )
        assert out["child_session_id"] == "c1"
        assert out["child_role"] == "worker"

    def test_subagent_stop_close(self):
        # tools/delegate_tool_results.py:349-372
        out = metrics.delegation_metrics_from_subagent_stop(
            {
                "parent_session_id": "p1",
                "parent_turn_id": "turn_1",
                "child_session_id": "c1",
                "child_role": "worker",
                "child_summary": "done",
                "child_status": "completed",
                "tool_call_history": [],
                "duration_ms": 4200,
            }
        )
        assert out["child_status"] == "completed"
        assert out["duration_ms"] == 4200


class TestDispatchMetrics:
    def test_worker_spawned(self):
        out = metrics.dispatch_metrics_from_kanban_worker_spawned(
            {
                "task_id": "t_1",
                "profile_name": "worker",
                "board": "b",
                "assignee": "worker",
                "run_id": 7,
                "worker_pid": 1900,
                "workspace_path": "C:/tmp/wt/t_1",
            }
        )
        assert out["worker_pid"] == 1900
        assert out["profile/role"] == "worker"
        assert out["workspace_path"] == "C:/tmp/wt/t_1"

    def test_worker_exited_fields(self):
        out = metrics.dispatch_metrics_from_kanban_worker_exited(KANBAN_WORKER_EXITED_KWARGS)
        assert out["exit_kind"] == "nonzero_exit"
        assert out["exit_code"] == 1
        assert out["outcome"] == "failed"
        assert out["retry_status"] == "retrying"

    def test_worker_exited_null_exit_code_dropped(self):
        out = metrics.dispatch_metrics_from_kanban_worker_exited(
            {"task_id": "t_1", "exit_kind": "signaled", "exit_code": None}
        )
        assert "exit_code" not in out

    def test_stale_claim(self):
        out = metrics.dispatch_metrics_from_kanban_worker_stale_claim(
            {
                "task_id": "t_1",
                "run_id": 3,
                "worker_pid": 5,
                "heartbeat_stale": True,
                "retry_status": "requeued",
            }
        )
        assert out["heartbeat_stale"] is True
        assert out["run_id"] == 3

    def test_task_updated_changed_fields_names_only(self):
        out = metrics.dispatch_metrics_from_kanban_task_updated(
            {
                "task_id": "t_1",
                "board": "b",
                "run_id": 2,
                "changed_fields": ["priority", "assignee"],
            }
        )
        assert out["changed_fields"] == ["priority", "assignee"]


# ---------------------------------------------------------------------------
# GAPS list integrity
# ---------------------------------------------------------------------------


class TestGaps:
    def test_gap_entries_are_well_formed(self):
        assert len(metrics.GAPS) == 12
        for gap in metrics.GAPS:
            assert set(gap) == {"metric", "reason", "needed_hook"}
            assert gap["reason"] and gap["needed_hook"]

    @pytest.mark.parametrize(
        "metric",
        [
            "envelope.key_id",
            "envelope.parent_task_ids",
            "envelope.trace_id",
            "envelope.handoff_id",
            "envelope.worktree/branch",
            "envelope.call_class = 'aux'",
            "envelope.call_class = 'judge'",
            "envelope.call_class = 'fallback'",
            "active_time.cumulative",
            "review.claimed / review.decision",
            "artifact.metrics",
        ],
    )
    def test_named_gaps_present(self, metric):
        assert any(g["metric"] == metric for g in metrics.GAPS), metric
