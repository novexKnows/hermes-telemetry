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
# Pinned correlation-envelope schema properties (v1.0.0, sha256 5271d5fa…33e70a,
# C:/tmp/sovav-hermes/r-obs-00/docs/telemetry/correlation-envelope.schema.json).
# Verbatim property list — used to assert the attribution output-key subset
# contract (schema declares additionalProperties: false).
# ---------------------------------------------------------------------------
SCHEMA_PROPERTIES = frozenset(
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

    def test_attribution_keys_are_subset_of_schema_properties(self):
        # Schema declares additionalProperties: false — non-schema hook kwargs
        # (assignee, summary, reason) must never ride on envelope attribution.
        kwargs = {
            **KANBAN_CLAIMED_KWARGS,
            "summary": "implemented metrics extraction",
            "reason": "needs_input: waiting on creds",
        }
        for fn in (
            metrics.envelope_attribution_from_kanban_claimed,
            metrics.envelope_attribution_from_kanban_completed,
            metrics.envelope_attribution_from_kanban_blocked,
        ):
            assert set(fn(kwargs)) <= SCHEMA_PROPERTIES, fn.__name__

    def test_completed_and_blocked_emit_no_non_schema_metadata(self):
        # summary/reason/assignee live on the event payload, not the envelope.
        for fn in (
            metrics.envelope_attribution_from_kanban_completed,
            metrics.envelope_attribution_from_kanban_blocked,
        ):
            out = fn(
                {
                    **KANBAN_CLAIMED_KWARGS,
                    "summary": "implemented metrics extraction",
                    "reason": "needs_input: waiting on creds",
                }
            )
            assert "summary" not in out
            assert "reason" not in out
            assert "assignee" not in out
            assert out["task_id"] == "t_77d64af1"

    def test_event_metadata_separated_from_attribution(self):
        kwargs = {
            **KANBAN_CLAIMED_KWARGS,
            "summary": "implemented metrics extraction",
            "reason": "needs_input: waiting on creds",
        }
        out = metrics.event_metadata_from_kanban_lifecycle(kwargs)
        assert out == {
            "assignee": "worker",
            "summary": "implemented metrics extraction",
            "reason": "needs_input: waiting on creds",
        }
        # ...and it is exactly the set of keys attribution excludes.
        attribution = metrics.envelope_attribution_from_kanban_completed(kwargs)
        assert not (set(out) & set(attribution))

    def test_blocked_missing_fields_are_dropped(self):
        out = metrics.envelope_attribution_from_kanban_blocked(
            {"task_id": "t_x", "run_id": None, "reason": "needs_input: waiting on creds"}
        )
        assert "run_id" not in out  # None dropped, not coerced to 0
        assert "board" not in out
        assert "reason" not in out

    def test_run_id_string_is_coerced_to_int(self):
        out = metrics.envelope_attribution_from_kanban_claimed({"task_id": "t_x", "run_id": "42"})
        assert out["run_id"] == 42


# ---------------------------------------------------------------------------
# task_id integrity — hook task_id is a per-turn UUID (agent/turn_context.py:489),
# NEVER the kanban card id; it must never ride the canonical envelope key.
# ---------------------------------------------------------------------------


class TestTaskIdIntegrity:
    def test_per_turn_uuid_never_populates_canonical_task_id(self):
        per_turn_uuid = "0f2f1e5a-1111-4c33-9c33-3f9f04686ba1"
        for fn in (
            metrics.call_context_from_pre_llm_call,
            metrics.call_context_from_post_api_request,
            metrics.tool_call_metrics_from_post_tool_call,
            metrics.api_error_metrics_from_api_request_error,
        ):
            out = fn({"task_id": per_turn_uuid, "turn_id": "turn_1", "model": "m"})
            assert "task_id" not in out, fn.__name__
            assert out.get("hook_task_id") == per_turn_uuid, fn.__name__

    def test_post_api_request_fixture_uuid_lands_on_hook_task_id(self):
        out = metrics.call_context_from_post_api_request(POST_API_REQUEST_KWARGS)
        assert "task_id" not in out
        assert out["hook_task_id"] == POST_API_REQUEST_KWARGS["task_id"]


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

    def test_moa_references_projected_not_passed_through(self):
        # REAL v0.21.3 item shape: agent/moa_trace.py slot_metrics (:64-72) over
        # _slot_trace (:52-61), built at agent/moa_loop.py:1216-1219 with
        # output=text — the advisor answer text rides on every real item.
        refs = [
            {
                "label": "advisor-a",
                "model": "openai/gpt-5.6-sol",
                "provider": "openrouter",
                "temperature": 0.7,
                "output": "advisor answer text — never exportable (telemetry-contract §3.1)",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 5,
                },
                "cost_usd": 0.0012,
                "cost_status": "ok",
                "cost_source": "provider",
            }
        ]
        out = metrics.billing_metrics_from_post_api_request(
            {"response": {}, "moa_references": refs}
        )
        advisors = out["moa_advisors"]
        assert advisors == [
            {
                "label": "advisor-a",
                "model": "openai/gpt-5.6-sol",
                "provider": "openrouter",
                "temperature": 0.7,
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 5,
                "cost_usd": 0.0012,
                "cost_status": "ok",
                "cost_source": "provider",
            }
        ]
        # no raw pass-through key, and no advisor text anywhere in the output
        assert "moa_references" not in out
        assert "output" not in advisors[0]
        assert "advisor answer text" not in str(out)

    def test_moa_projection_drops_anything_outside_the_allowlist(self):
        refs = [
            {
                "label": "advisor-b",
                "output": "text",
                "input_messages": [{"role": "user", "content": "secret prompt body"}],
                "some_future_field": "x",
                "usage": {"input_tokens": 1, "request_count": 2},
            }
        ]
        advisors = metrics.moa_metrics_from_moa_references(refs)
        assert advisors == [{"label": "advisor-b", "input_tokens": 1}]
        assert "request_count" not in advisors[0]  # not a moa usage bucket

    def test_moa_projection_none_and_malformed(self):
        assert metrics.moa_metrics_from_moa_references(None) is None
        assert metrics.moa_metrics_from_moa_references("nope") is None
        assert metrics.moa_metrics_from_moa_references([None, "junk", {"label": "ok"}]) == [
            {"label": "ok"}
        ]

    def test_billing_moa_empty_or_absent_emits_no_key(self):
        assert metrics.billing_metrics_from_post_api_request({"response": {}}) == {}
        assert (
            metrics.billing_metrics_from_post_api_request({"response": {}, "moa_references": []})
            == {}
        )
        assert (
            metrics.billing_metrics_from_post_api_request({"response": {}, "moa_references": None})
            == {}
        )


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
        assert len(metrics.GAPS) == 13
        for gap in metrics.GAPS:
            assert set(gap) == {"metric", "reason", "needed_hook"}
            assert gap["reason"] and gap["needed_hook"]

    @pytest.mark.parametrize(
        "metric",
        [
            "envelope.key_id",
            "envelope.parent_task_ids",
            "failed_attempt.tokens/cost",
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

    def test_failed_attempt_gap_covers_tokens_and_cost(self):
        gap = next(g for g in metrics.GAPS if g["metric"] == "failed_attempt.tokens/cost")
        assert "usage" in gap["needed_hook"] and "billed_cost_usd" in gap["needed_hook"]
        assert "api_request_error" in gap["reason"]

    def test_failover_and_key_attribution_gaps_cover_credential_rotation(self):
        key_gap = next(g for g in metrics.GAPS if g["metric"] == "envelope.key_id")
        assert "credential rotation" in key_gap["reason"]
        fallback_gap = next(
            g for g in metrics.GAPS if g["metric"] == "envelope.call_class = 'fallback'"
        )
        assert "credential" in fallback_gap["reason"]
        assert "credential-rotation" in fallback_gap["needed_hook"]


# ---------------------------------------------------------------------------
# agent_loop_stopped interrupt observer (sourceable — mapped, not a gap)
# ---------------------------------------------------------------------------


class TestInterruptMetrics:
    def test_gateway_fire_site_kwargs(self):
        # gateway/run_agent_cache.py:492-498
        out = metrics.interrupt_metrics_from_agent_loop_stopped(
            {
                "session_key": "telegram:chat:123",
                "platform": "telegram",
                "reason": "user_requested",
                "invalidation_reason": "session_interrupt",
            }
        )
        assert out == {
            "session_key": "telegram:chat:123",
            "platform": "telegram",
            "interrupt_reason": "user_requested",
            "invalidation_reason": "session_interrupt",
        }

    def test_tui_fire_site_kwargs(self):
        # tui_gateway/session_lifecycle.py:614-617
        out = metrics.interrupt_metrics_from_agent_loop_stopped(
            {
                "session_key": "20260919_101500_abc123",
                "platform": "tui",
                "reason": "user_stop",
                "invalidation_reason": "session_interrupt",
            }
        )
        assert out["interrupt_reason"] == "user_stop"

    def test_missing_reasons_dropped_and_key_is_session_key(self):
        out = metrics.interrupt_metrics_from_agent_loop_stopped({"session_key": "k"})
        assert out == {"session_key": "k"}
