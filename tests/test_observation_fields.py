"""Observation fields (v17): kanban IDs, retry/request IDs, upstream provider,
provider generation id, billed cost + billing source/status, long-context
pricing override — captured on llm_calls from hook payloads and the kanban env.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_init_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_init_mod)


class MockPluginContext:
    """Minimal stand-in for Hermes PluginContext."""

    def __init__(self, profile_name="default"):
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


@pytest.fixture()
def telemetry_db(monkeypatch, tmp_path):
    """Isolated DB under tmp_path + reset module-level observation state."""
    import hermes_telemetry.db as db_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_TELEMETRY_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    db_mod._local.conn = None
    _init_mod._retry_counts.clear()
    _init_mod._tier_thresholds.clear()
    _init_mod._pricing_snapshot_seen.clear()
    yield db_mod
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None
    _init_mod._retry_counts.clear()
    _init_mod._tier_thresholds.clear()


def _make_ctx():
    ctx = MockPluginContext()
    _init_mod.register(ctx)
    return ctx


def _post_kwargs(**overrides):
    kw = dict(
        session_id="obs1",
        model="claude-sonnet-4-6",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        api_duration=1.0,
        usage={
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
        response_model="claude-sonnet-4-6",
        api_call_count=1,
        assistant_content_chars=10,
    )
    kw.update(overrides)
    return kw


def _last_llm_row(db, session_id="obs1"):
    conn = db._get_conn()
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute(
        "SELECT * FROM llm_calls WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def test_post_api_request_captures_api_request_id_and_kanban_env(telemetry_db, monkeypatch):
    # Exactly the variable names the kanban dispatcher exports for the worker
    # process (hermes_cli/kanban_db_dispatch.py): HERMES_KANBAN_TASK,
    # HERMES_KANBAN_RUN_ID, HERMES_KANBAN_BOARD.
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "turnstone")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")

    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs(api_request_id="req-xyz"))

    row = _last_llm_row(telemetry_db)
    assert row["api_request_id"] == "req-xyz"
    assert row["kanban_board"] == "turnstone"
    assert row["kanban_task"] == "t_abc123"
    assert row["kanban_run"] == "77"
    # No errors observed -> retry_count stays NULL
    assert row["retry_count"] is None


def test_hook_task_uuid_cannot_overwrite_kanban_card(telemetry_db, monkeypatch):
    # run_conversation generates a per-turn UUID for the hook's task_id kwarg
    # when no task was supplied (agent/turn_context.py). That UUID is NOT a
    # Kanban card id and must never overwrite the dispatcher-exported value.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_card123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "55")
    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs(task_id=str(uuid.uuid4())))
    row = _last_llm_row(telemetry_db)
    assert row["kanban_task"] == "t_card123"
    assert row["kanban_run"] == "55"


def test_hook_task_id_without_kanban_env_leaves_kanban_null(telemetry_db):
    # A non-Kanban run (e.g. plain `hermes` CLI) still fires the hook with a
    # generated task UUID — the kanban columns must stay NULL there.
    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs(task_id=str(uuid.uuid4())))
    row = _last_llm_row(telemetry_db)
    assert row["kanban_board"] is None
    assert row["kanban_task"] is None
    assert row["kanban_run"] is None


def test_no_kanban_context_leaves_columns_null(telemetry_db):
    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs())
    row = _last_llm_row(telemetry_db)
    assert row["kanban_board"] is None
    assert row["kanban_task"] is None
    assert row["kanban_run"] is None
    assert row["api_request_id"] is None


def test_response_payload_fields_captured(telemetry_db):
    ctx = _make_ctx()
    ctx.fire(
        "post_api_request",
        **_post_kwargs(
            response={
                "model": "claude-sonnet-4-6",
                "id": "gen-12345",
                "provider": "Anthropic",
                "billed_cost_usd": 0.0042,
            }
        ),
    )
    row = _last_llm_row(telemetry_db)
    assert row["provider_generation_id"] == "gen-12345"
    assert row["upstream_provider"] == "Anthropic"
    assert row["billed_cost_usd"] == pytest.approx(0.0042)
    assert row["billing_source"] == "provider"
    assert row["billing_status"] == "reported"


def test_non_numeric_billed_cost_stays_null(telemetry_db):
    ctx = _make_ctx()
    ctx.fire(
        "post_api_request",
        **_post_kwargs(response={"id": "gen-1", "billed_cost_usd": "not-a-number"}),
    )
    row = _last_llm_row(telemetry_db)
    assert row["provider_generation_id"] == "gen-1"
    assert row["billed_cost_usd"] is None
    assert row["billing_source"] is None
    assert row["billing_status"] is None


def test_absent_response_payload_leaves_upstream_fields_null(telemetry_db):
    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs())
    row = _last_llm_row(telemetry_db)
    assert row["upstream_provider"] is None
    assert row["provider_generation_id"] is None
    assert row["billed_cost_usd"] is None


def test_retry_count_counts_errors_then_resets(telemetry_db):
    ctx = _make_ctx()
    ctx.fire(
        "api_request_error",
        session_id="obs1",
        status_code=500,
        retryable=True,
        error_type="server",
        error_message="boom",
    )
    ctx.fire(
        "api_request_error",
        session_id="obs1",
        status_code=429,
        retryable=True,
        error_type="rate_limit",
        error_message="slow down",
    )
    ctx.fire("post_api_request", **_post_kwargs())
    row = _last_llm_row(telemetry_db)
    assert row["retry_count"] == 2

    # Cleared after recording: the next clean call records NULL again.
    ctx.fire("post_api_request", **_post_kwargs(api_call_count=2))
    row = _last_llm_row(telemetry_db)
    assert row["retry_count"] is None


def test_long_context_override_flagged_when_threshold_exceeded(telemetry_db):
    # Seed the tier-threshold cache the way the snapshot capture would.
    _init_mod._tier_thresholds[("openrouter", "claude-sonnet-4-6")] = 200_000
    ctx = _make_ctx()
    ctx.fire(
        "post_api_request",
        **_post_kwargs(usage={"input_tokens": 250_000, "output_tokens": 10}),
    )
    row = _last_llm_row(telemetry_db)
    assert row["long_context_override"] == 1


def test_long_context_zero_when_below_threshold(telemetry_db):
    _init_mod._tier_thresholds[("openrouter", "claude-sonnet-4-6")] = 200_000
    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs())
    row = _last_llm_row(telemetry_db)
    assert row["long_context_override"] == 0


def test_long_context_null_without_known_tier(telemetry_db):
    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs())
    row = _last_llm_row(telemetry_db)
    assert row["long_context_override"] is None


def _tiered_resolve(threshold):
    """Stand-in for core_pricing.resolve_with_fallback returning a tiered tariff."""

    def _fake(model, provider="", base_url=""):
        snap = {
            "input_cost_per_million": 3.0,
            "output_cost_per_million": 15.0,
            "tier_threshold_tokens": threshold,
        }
        return snap, None

    return _fake


def test_long_context_override_on_cold_cache_first_call_exceeded(telemetry_db, monkeypatch):
    # Regression: the very first call for a (provider, model) has an empty
    # _tier_thresholds cache. The tariff must be resolved (and its tier
    # threshold stored) BEFORE the llm_calls row is written, so this first row
    # already carries the override flag instead of a misleading NULL.
    import hermes_telemetry.core_pricing as core_pricing_mod

    monkeypatch.setattr(
        core_pricing_mod, "resolve_with_fallback", _tiered_resolve(200_000)
    )
    ctx = _make_ctx()
    ctx.fire(
        "post_api_request",
        **_post_kwargs(usage={"input_tokens": 250_000, "output_tokens": 10}),
    )
    row = _last_llm_row(telemetry_db)
    assert row["long_context_override"] == 1


def test_long_context_override_on_cold_cache_first_call_below(telemetry_db, monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing_mod

    monkeypatch.setattr(
        core_pricing_mod, "resolve_with_fallback", _tiered_resolve(200_000)
    )
    ctx = _make_ctx()
    ctx.fire("post_api_request", **_post_kwargs())
    row = _last_llm_row(telemetry_db)
    assert row["long_context_override"] == 0


def test_pricing_resolve_failure_keeps_cold_cache_null(telemetry_db, monkeypatch):
    # Fail-open guard: a pricing failure must not affect the recorded call —
    # the override simply stays NULL.
    import hermes_telemetry.core_pricing as core_pricing_mod

    def _boom(model, provider="", base_url=""):
        raise RuntimeError("pricing backend down")

    monkeypatch.setattr(core_pricing_mod, "resolve_with_fallback", _boom)
    ctx = _make_ctx()
    ctx.fire(
        "post_api_request",
        **_post_kwargs(usage={"input_tokens": 250_000, "output_tokens": 10}),
    )
    row = _last_llm_row(telemetry_db)
    assert row["session_id"] == "obs1"  # the call itself was still recorded
    assert row["long_context_override"] is None
