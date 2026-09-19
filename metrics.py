"""Pure metric extraction for the sovav-hermes utilization controller (HTB-B2).

Every function here takes ONE hook's kwargs (as a dict) and returns a plain
dict whose keys use the ratified R-OBS-00 correlation-envelope field names
(``profile/role``, ``worktree/branch`` — slashes included verbatim) or the
controller metric names documented in
``docs/telemetry/utilization-metrics-mapping.md``.

Boundaries (module ownership, TM card t_77d64af1):
- No envelope construction (B1 owns envelope.py).
- No persistence, no I/O, no transport (B3 owns transport.py).
- No imports from sibling stream modules; stdlib only.

Every kwarg name consumed below was verified against the INSTALLED v0.21.3
Hermes source (C:/Users/Sovav/AppData/Local/hermes/hermes-agent, commit
0ddba07ad7, version 0.21.3) — see the mapping doc for file:line evidence.
EVIDENCE BASIS: the installed tree is dirty — response ``id``/``provider``/
``billed_cost_usd`` (agent/api_request_hooks.py:148-163) and a projection-body
merge in agent/auxiliary_client.py are UNCOMMITTED changes absent from commit
0ddba07ad7. Those fields are evidenced against the dirty installed tree, NOT
the commit (see docs/telemetry/utilization-metrics-mapping.md § Source
provenance). Unknown kwargs are simply ignored; callers pass ``**hook_kwargs``
dicts freely.

Controller metrics the v0.21.3 hooks CANNOT source are listed in ``GAPS``
(never invented here; each gap names the evidence and the hook/feature that
would be needed).
"""

import re
from typing import Any, Dict, List, Optional

__all__ = [
    "call_class_from_session_context",
    "api_error_metrics_from_api_request_error",
    "billing_metrics_from_post_api_request",
    "call_context_from_pre_llm_call",
    "call_context_from_post_api_request",
    "cron_job_id_from_session_id",
    "delegation_metrics_from_subagent_start",
    "delegation_metrics_from_subagent_stop",
    "dispatch_metrics_from_kanban_task_updated",
    "dispatch_metrics_from_kanban_worker_exited",
    "dispatch_metrics_from_kanban_worker_spawned",
    "dispatch_metrics_from_kanban_worker_stale_claim",
    "envelope_attribution_from_kanban_blocked",
    "envelope_attribution_from_kanban_claimed",
    "envelope_attribution_from_kanban_completed",
    "event_metadata_from_kanban_lifecycle",
    "session_outcome_from_on_session_end",
    "interrupt_metrics_from_agent_loop_stopped",
    "moa_metrics_from_moa_references",
    "tool_call_metrics_from_post_tool_call",
    "usage_metrics_from_post_api_request",
    "CALL_CLASS_VALUES",
    "GAPS",
]

# Envelope ``call_class`` enum, verbatim from correlation-envelope.schema.json
# (urn:sovav-hermes:telemetry:correlation-envelope:1.0.0, version 1.0.0).
CALL_CLASS_VALUES = ("primary", "fallback", "aux", "delegate", "judge", "cron")

# Anchored cron session-id shape, verified at cron/scheduler.py:2302
# (_cron_session_id = f"cron_{job_id}_{YYYYMMDD_HHMMSS}"). Anchored so job ids
# containing underscores stay intact (never split("_")[1]); the greedy job-id
# group gives the trailing 8-digit date + 6-digit time priority.
_CRON_SESSION_RE = re.compile(r"^cron_(?P<job_id>.+)_(?P<date>\d{8})_(?P<time>\d{6})$")

# Explicit v0.21.3 sourcing gaps. ``reason`` names the evidence that the metric
# is missing; ``needed_hook`` names the smallest hook/feature that would close
# it. Consumed by docs/telemetry/utilization-metrics-mapping.md and asserted in
# tests/test_metrics.py so the list cannot silently rot.
GAPS: List[Dict[str, str]] = [
    {
        "metric": "envelope.key_id",
        "reason": (
            "No hook kwarg carries a non-secret credential label, so credential rotation "
            "and failover cannot be attributed to a key. Request/response hook payloads "
            "sanitize credential values to '<redacted>' "
            "(agent/api_request_hooks.py:60-64,95) but expose no key NAME or hash prefix "
            "and no rotation/failover decision."
        ),
        "needed_hook": (
            "post_api_request / pre_api_request kwarg key_id (label only) plus a "
            "rotation/failover decision label, sourced from the gateway's "
            "credential-rotation layer"
        ),
    },
    {
        "metric": "envelope.parent_task_ids",
        "reason": (
            "No kanban lifecycle hook carries the parent-card set: "
            "kanban_task_claimed/completed/blocked fire task_id, board, assignee, run_id, "
            "profile_name only (hermes_cli/kanban_db.py:165-172,2288,2803,3172-3174)."
        ),
        "needed_hook": "kanban_task_claimed kwarg parent_task_ids: list[str]",
    },
    {
        "metric": "failed_attempt.tokens/cost",
        "reason": (
            "Failed/billed-attempt accounting is missing: api_request_error fires error "
            "metadata only (status/retry context, agent/api_request_hooks.py:175-210) with "
            "no usage or billed cost, and _usage_summary_for_api_request_hook + "
            "_api_response_payload_for_hook cover successful responses only "
            "(agent/api_request_hooks.py:35-49,142-173). A failed attempt therefore has no "
            "token or cost payload at all."
        ),
        "needed_hook": (
            "api_request_error kwarg usage + billed_cost_usd (partial-attempt accounting)"
        ),
    },
    {
        "metric": "envelope.trace_id",
        "reason": (
            "No hook carries a cross-repo trace identifier. pre_api_request.middleware_trace "
            "(agent/turn_api_request.py:85) is the LLM request-middleware trace, not a trace id."
        ),
        "needed_hook": "post_api_request kwarg trace_id (or a documented LangSmith run-id bridge)",
    },
    {
        "metric": "envelope.handoff_id",
        "reason": (
            "No hook kwarg identifies a worker→reviewer / card→card handoff. "
            "kanban_task_completed carries a free-text summary only "
            "(hermes_cli/kanban_db.py:2803)."
        ),
        "needed_hook": "kanban_task_completed / kanban_task_blocked kwarg handoff_id",
    },
    {
        "metric": "envelope.worktree/branch",
        "reason": (
            "No hook kwarg carries the git branch. on_kanban_worker_spawned carries "
            "workspace_path — a filesystem path, not a branch "
            "(hermes_cli/kanban_db.py:193-207; privacy-noted at "
            "hermes_cli/kanban_db_dispatch.py:1289-1290)."
        ),
        "needed_hook": "kanban_task_claimed / on_kanban_worker_spawned kwarg branch",
    },
    {
        "metric": "envelope.call_class = 'fallback'",
        "reason": (
            "Fallback routing and failover decisions (provider failover, credential "
            "rotation) are not exposed as a class or as a decision id. "
            "post_api_request.response.provider (agent/api_request_hooks.py:148-163, dirty "
            "installed tree) reveals the upstream that actually served the request, but "
            "nothing marks the call as a fallback path or names the rotation/failover decision."
        ),
        "needed_hook": (
            "post_api_request kwarg call_class or failover_reason "
            "(including credential-rotation/failover decision attribution)"
        ),
    },
    {
        "metric": "envelope.call_class = 'aux'",
        "reason": (
            "Auxiliary-path calls (MoA reference models, one-shot /moa) run through "
            "agent/auxiliary_client.py::call_llm, which contains no invoke_hook dispatch — no "
            "hook fires, so no kwarg exists to classify. post_api_request.moa_references "
            "(agent/turn_response_intake.py:92; agent/conversation_loop.py:270-280) exposes the "
            "AGGREGATOR-generation per-advisor metrics only."
        ),
        "needed_hook": "hook dispatch inside agent/auxiliary_client.py::call_llm",
    },
    {
        "metric": "envelope.call_class = 'judge'",
        "reason": (
            "Goal-mode judge runs are not hooked: no hook name in "
            "hermes_cli/plugins.py VALID_HOOKS (lines 108-200) covers the judge path."
        ),
        "needed_hook": "a judge-lifecycle observer hook carrying the judged task/run ids",
    },
    {
        "metric": "active_time.cumulative",
        "reason": (
            "Cumulative active time lives inside agent/activity_tracking.py "
            "(_touch_activity liveness clock) and is bridged internally to the kanban "
            "heartbeat; no hook kwarg exposes it. Per-request active time IS sourceable "
            "(post_api_request.api_duration)."
        ),
        "needed_hook": "on_session_end kwarg active_seconds (cumulative liveness clock)",
    },
    {
        "metric": "review.claimed / review.decision",
        "reason": (
            "claim_review_task (hermes_cli/kanban_db.py:2292) fires NO lifecycle hook — review-lane "
            "claims and review verdicts (approve / request_changes) are invisible. Only "
            "kanban_task_completed.summary is observable after the fact."
        ),
        "needed_hook": "kanban_task_review_claimed + review verdict observer "
        "(or kanban_task_claimed fired on the review lane)",
    },
    {
        "metric": "artifact.metrics",
        "reason": (
            "No hook reports artifacts. Hermes has no artifact store; artifacts are references "
            "parsed from session transcripts (ONBOARDING.md § Hermes Session Storage) — outside "
            "the hook kwarg surface this module extracts from."
        ),
        "needed_hook": "a post_tool_call enrichment naming artifact paths written by the tool",
    },
    {
        "metric": "tokens when usage is None",
        "reason": (
            "post_api_request.usage is None when the provider returns no usage info "
            "(agent/api_request_hooks.py:35-41); only approx_input_tokens from pre_api_request "
            "(agent/turn_api_request.py:81) supports an estimate, and estimation is the "
            "caller's policy, not extraction."
        ),
        "needed_hook": "provider-side guaranteed usage reporting (upstream, not a plugin hook)",
    },
]


def _int(value: Any) -> Optional[int]:
    """Best-effort int coercion; None passes through, junk returns None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    """Best-effort float coercion; None passes through, junk returns None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _put(out: Dict[str, Any], key: str, value: Any) -> None:
    """Insert only when the value is present (None drops the key)."""
    if value is not None:
        out[key] = value


# ---------------------------------------------------------------------------
# Envelope attribution from kanban lifecycle hooks
# (hermes_cli/kanban_db.py:151-172, 2288, 2803, 3172-3174 — common kwargs
# task_id, board, assignee, run_id, profile_name; completed adds summary,
# blocked adds reason.)
#
# OUTPUT CONTRACT: envelope-attribution output keys are a strict SUBSET of the
# pinned correlation-envelope schema properties (v1.0.0,
# additionalProperties: false). Non-schema hook kwargs — assignee, summary,
# reason — are EVENT payload metadata, never envelope attribution; they are
# extracted separately by event_metadata_from_kanban_lifecycle and joined by
# task_id + run_id. Asserted by tests (attribution keys ⊆ schema properties).
# ---------------------------------------------------------------------------


def envelope_attribution_from_kanban_claimed(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """kanban_task_claimed kwargs → envelope base fields sourceable at claim time.

    Emits ONLY correlation-envelope schema properties (board, task_id, run_id,
    profile/role). ``assignee`` is not a schema property — it is event payload
    metadata (event_metadata_from_kanban_lifecycle).
    """
    out: Dict[str, Any] = {}
    _put(out, "board", kwargs.get("board"))
    _put(out, "task_id", kwargs.get("task_id"))
    _put(out, "run_id", _int(kwargs.get("run_id")))
    _put(out, "profile/role", kwargs.get("profile_name"))
    return out


def envelope_attribution_from_kanban_completed(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """kanban_task_completed kwargs → envelope-attributable fields at completion.

    Today identical to claim time: ``summary`` is free text, not an envelope
    schema property — it stays on the event payload
    (event_metadata_from_kanban_lifecycle) until the schema grows a
    ``handoff_id``/summary carrier (see GAPS: envelope.handoff_id).
    """
    return envelope_attribution_from_kanban_claimed(kwargs)


def envelope_attribution_from_kanban_blocked(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """kanban_task_blocked kwargs → envelope-attributable fields at block time.

    ``reason`` is not an envelope schema property — event payload metadata
    (event_metadata_from_kanban_lifecycle), never envelope attribution.
    """
    return envelope_attribution_from_kanban_claimed(kwargs)


def event_metadata_from_kanban_lifecycle(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Kanban lifecycle EVENT payload metadata that is NOT envelope-shaped.

    ``assignee`` (claimed/completed/blocked), ``summary`` (completed) and
    ``reason`` (blocked) are not correlation-envelope schema properties
    (additionalProperties: false). They ride on the controller event payload,
    joined by envelope task_id + run_id — never merged into the envelope.
    """
    out: Dict[str, Any] = {}
    _put(out, "assignee", kwargs.get("assignee"))
    _put(out, "summary", kwargs.get("summary"))
    _put(out, "reason", kwargs.get("reason"))
    return out


def cron_job_id_from_session_id(session_id: str) -> Optional[str]:
    """Extract the cron job id from a cron session id; None for non-cron sessions.

    Session format verified at cron/scheduler.py:2302:
    ``cron_{job_id}_{YYYYMMDD_HHMMSS}``. Anchored match so job ids containing
    underscores survive intact (never ``split("_")[1]``).
    """
    if not session_id:
        return None
    match = _CRON_SESSION_RE.match(session_id)
    if not match:
        return None
    return match.group("job_id") or None


# ---------------------------------------------------------------------------
# call_class inference — only the classes the hooks actually evidence.
# Never invents "fallback"/"aux"/"judge" (see GAPS).
# ---------------------------------------------------------------------------


def call_class_from_session_context(
    platform: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    session_id: str = "",
) -> Optional[str]:
    """Derive ``call_class`` from the session evidence hooks carry.

    - "cron"     — platform == "cron" (on_session_start/on_session_end platform
                   kwarg) or the session_id cron prefix (cron/scheduler.py:2302).
    - "delegate" — a non-empty parent_session_id (pre_llm_call carries
                   parent_session_id, agent/turn_context.py:703; child sessions
                   of delegate_task have a parent).
    - None       — otherwise. "primary" is a caller-level default, not inferred
                   here; "fallback"/"aux"/"judge" are unsourceable (GAPS).
    """
    if platform == "cron" or cron_job_id_from_session_id(session_id or "") is not None:
        return "cron"
    if parent_session_id:
        return "delegate"
    return None


# ---------------------------------------------------------------------------
# LLM call context + usage (post_api_request is the primary token hook)
# ---------------------------------------------------------------------------


def call_context_from_pre_llm_call(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """pre_llm_call kwargs (agent/turn_context.py:693-706) → call-context metrics.

    Carries NO token data (fires before the API call loop) — only identity.
    Hook ``task_id`` is a per-turn UUID (``effective_task_id = task_id or
    str(uuid.uuid4())``, agent/turn_context.py:489) — NOT the kanban card id.
    It is exposed as ``hook_task_id`` so it can never collide with the canonical
    envelope ``task_id`` (the board card id).
    """
    out: Dict[str, Any] = {}
    _put(out, "model", kwargs.get("model"))
    _put(out, "platform", kwargs.get("platform"))
    _put(out, "hook_task_id", kwargs.get("task_id"))
    _put(out, "turn_id", kwargs.get("turn_id"))
    _put(out, "sender_id", kwargs.get("sender_id"))
    _put(out, "parent_session_id", kwargs.get("parent_session_id"))
    if kwargs.get("is_first_turn") is not None:
        out["is_first_turn"] = bool(kwargs.get("is_first_turn"))
    call_class = call_class_from_session_context(
        platform=kwargs.get("platform"),
        parent_session_id=kwargs.get("parent_session_id"),
    )
    _put(out, "call_class", call_class)
    return out


def call_context_from_post_api_request(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """post_api_request kwargs (agent/turn_response_intake.py:62-93) → call identity.

    Hook ``task_id`` is the per-turn UUID (agent/turn_context.py:489) — exposed
    as ``hook_task_id``, never under canonical ``task_id``.
    """
    out: Dict[str, Any] = {}
    _put(out, "model", kwargs.get("model"))
    _put(out, "provider", kwargs.get("provider"))
    _put(out, "platform", kwargs.get("platform"))
    _put(out, "hook_task_id", kwargs.get("task_id"))
    _put(out, "turn_id", kwargs.get("turn_id"))
    _put(out, "api_request_id", kwargs.get("api_request_id"))
    _put(out, "api_call_count", _int(kwargs.get("api_call_count")))
    _put(out, "finish_reason", kwargs.get("finish_reason"))
    _put(out, "response_model", kwargs.get("response_model"))
    return out


def usage_metrics_from_post_api_request(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """post_api_request kwargs → token + latency metrics.

    ``usage`` shape verified at agent/api_request_hooks.py:35-49 over
    CanonicalUsage (agent/usage_pricing.py:56-71): input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, reasoning_tokens, request_count,
    prompt_tokens (property = input + cache_read + cache_write), total_tokens
    (property = prompt + output). ``usage`` is None when the provider reported
    nothing — in that case no token keys are emitted (estimation is the
    caller's policy; see GAPS).

    Latency: ``api_duration`` is seconds (converted to ms here);
    ``first_chunk_at`` (epoch s, None when not streamed) minus ``started_at``
    yields TTFB seconds.
    """
    out: Dict[str, Any] = {}
    usage = kwargs.get("usage")
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "prompt_tokens",
            "total_tokens",
        ):
            _put(out, key, _int(usage.get(key)))
        _put(out, "request_count", _int(usage.get("request_count")))
    api_duration_s = _float(kwargs.get("api_duration"))
    _put(
        out,
        "api_duration_ms",
        int(round(api_duration_s * 1000)) if api_duration_s is not None else None,
    )
    _put(out, "api_started_at", _float(kwargs.get("started_at")))
    _put(out, "api_ended_at", _float(kwargs.get("ended_at")))
    first_chunk_at = _float(kwargs.get("first_chunk_at"))
    started_at = _float(kwargs.get("started_at"))
    if first_chunk_at is not None and started_at is not None:
        _put(out, "time_to_first_token_s", max(0.0, first_chunk_at - started_at))
    _put(out, "assistant_content_chars", _int(kwargs.get("assistant_content_chars")))
    _put(out, "assistant_tool_call_count", _int(kwargs.get("assistant_tool_call_count")))
    return out


def billing_metrics_from_post_api_request(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """post_api_request.response payload → provider-reported billing metrics.

    ``response`` shape verified at agent/api_request_hooks.py:142-173 (DIRTY
    installed tree — the ``id``/``provider``/``billed_cost_usd`` keys are
    uncommitted changes absent from commit 0ddba07ad7): ``id`` (provider
    generation id), ``provider`` (the upstream that actually served the
    request, e.g. OpenRouter's routing field), ``billed_cost_usd``
    (provider-reported billed cost, present only when the provider reports
    one — OpenRouter does), ``model``, ``finish_reason``.

    ``moa_references`` is NOT passed through raw: each real item carries the
    advisor's answer text in ``output`` (agent/moa_trace.py:64-72,
    agent/moa_loop.py:1216-1219), violating the metadata-only export contract
    (telemetry-contract.md §3.1). It is reduced to the allowlisted projection
    from moa_metrics_from_moa_references under the ``moa_advisors`` key.
    """
    out: Dict[str, Any] = {}
    response = kwargs.get("response")
    if isinstance(response, dict):
        _put(out, "billed_cost_usd", _float(response.get("billed_cost_usd")))
        _put(out, "serving_provider", response.get("provider"))
        _put(out, "provider_response_id", response.get("id"))
    moa_metrics = moa_metrics_from_moa_references(kwargs.get("moa_references"))
    if moa_metrics:
        out["moa_advisors"] = moa_metrics
    return out


# Allowlisted projection of one post_api_request.moa_references item. Real item
# shape verified: agent/moa_trace.py slot_metrics (:64-72) over _slot_trace
# (:52-61), built for the hook at agent/moa_loop.py:1216-1219 — label, account
# identity (model/provider/temperature), a ``usage`` dict of token buckets,
# and cost status/source. ``output`` (the advisor's answer text — a prompt/
# tool-output-class body) and ``input_messages`` are explicitly DISCARDED:
# metadata-only contract, telemetry-contract.md §3.1/§3.2.
_MOA_ITEM_ALLOWLIST = (
    "label",
    "model",
    "provider",
    "temperature",
    "cost_usd",
    "cost_status",
    "cost_source",
)
# Token buckets inside the item's ``usage`` dict (agent/moa_trace.py:47).
_MOA_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def moa_metrics_from_moa_references(moa_references: Any) -> Optional[List[Dict[str, Any]]]:
    """post_api_request.moa_references → allowlisted per-advisor metric dicts.

    Per item, keeps ONLY: ``label``, ``model``, ``provider``, ``temperature``,
    ``cost_usd``, ``cost_status``, ``cost_source`` and the token buckets inside
    ``usage``. Everything else — critically ``output`` (advisor answer text)
    and ``input_messages`` — is dropped, never passed through. Items that are
    not dicts are skipped; a non-list payload or ``None`` yields ``None``/empty
    (no key is emitted by callers).
    """
    if moa_references is None or not isinstance(moa_references, (list, tuple)):
        return None
    projected: List[Dict[str, Any]] = []
    for item in moa_references:
        if not isinstance(item, dict):
            continue
        entry: Dict[str, Any] = {}
        for key in _MOA_ITEM_ALLOWLIST:
            _put(entry, key, item.get(key))
        usage = item.get("usage")
        if isinstance(usage, dict):
            for key in _MOA_USAGE_KEYS:
                _put(entry, key, _int(usage.get(key)))
        if entry:
            projected.append(entry)
    return projected


# ---------------------------------------------------------------------------
# Tool calls, API errors, session outcome
# ---------------------------------------------------------------------------


def tool_call_metrics_from_post_tool_call(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """post_tool_call kwargs (model_tools.py:676-695, _CallIds at 643-653) → tool metrics."""
    out: Dict[str, Any] = {}
    _put(out, "tool_name", kwargs.get("tool_name"))
    _put(out, "tool_call_id", kwargs.get("tool_call_id"))
    # Per-turn UUID (agent/turn_context.py:489) — never the kanban card id.
    _put(out, "hook_task_id", kwargs.get("task_id"))
    _put(out, "session_id", kwargs.get("session_id"))
    _put(out, "turn_id", kwargs.get("turn_id"))
    _put(out, "api_request_id", kwargs.get("api_request_id"))
    _put(out, "tool_duration_ms", _int(kwargs.get("duration_ms")))
    _put(out, "tool_status", kwargs.get("status"))
    _put(out, "error_type", kwargs.get("error_type"))
    _put(out, "error_message", kwargs.get("error_message"))
    return out


def api_error_metrics_from_api_request_error(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """api_request_error kwargs (agent/api_request_hooks.py:175-210) → error metrics.

    No usage/billed-cost kwarg exists on this hook (failed attempts carry no
    token/cost payload — GAPS: failed_attempt.tokens/cost).
    """
    out: Dict[str, Any] = {}
    _put(out, "model", kwargs.get("model"))
    _put(out, "provider", kwargs.get("provider"))
    # Per-turn UUID (agent/turn_context.py:489) — never the kanban card id.
    _put(out, "hook_task_id", kwargs.get("task_id"))
    _put(out, "turn_id", kwargs.get("turn_id"))
    _put(out, "api_request_id", kwargs.get("api_request_id"))
    _put(out, "status_code", _int(kwargs.get("status_code")))
    _put(out, "retryable", kwargs.get("retryable"))
    _put(out, "reason", kwargs.get("reason"))
    _put(out, "retry_count", _int(kwargs.get("retry_count")))
    _put(out, "max_retries", _int(kwargs.get("max_retries")))
    _put(out, "api_call_count", _int(kwargs.get("api_call_count")))
    error = kwargs.get("error")
    if isinstance(error, dict):
        _put(out, "error_type", error.get("type"))
        _put(out, "error_message", error.get("message"))
    api_duration_s = _float(kwargs.get("api_duration"))
    _put(
        out,
        "api_duration_ms",
        int(round(api_duration_s * 1000)) if api_duration_s is not None else None,
    )
    return out


def interrupt_metrics_from_agent_loop_stopped(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """agent_loop_stopped kwargs → interrupt observer metrics.

    Fire sites verified: gateway/run_agent_cache.py:492-498 (gateway
    /stop and running-agent fast path; the pending-sentinel /stop path stays
    silent) and tui_gateway/session_lifecycle.py:614-617 (TUI/desktop
    session.interrupt). Observer-only; return value ignored. Kwarg names are
    verbatim: session_key, platform, reason, invalidation_reason — ``reason``
    is the interrupt reason, NOT the kanban block reason.

    This is the sourceable run-interruption observer: it complements
    session_outcome_from_on_session_end (run outcome at session end) with the
    mid-run interrupt event; it carries session_key, not session_id, so the
    controller joins it via platform + session key correlation.
    """
    out: Dict[str, Any] = {}
    _put(out, "session_key", kwargs.get("session_key"))
    _put(out, "platform", kwargs.get("platform"))
    _put(out, "interrupt_reason", kwargs.get("reason"))
    _put(out, "invalidation_reason", kwargs.get("invalidation_reason"))
    return out


def session_outcome_from_on_session_end(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """on_session_end kwargs (agent/turn_finalizer.py:658-670) → run outcome.

    Status derivation matches the plugin's existing convention
    (ONBOARDING.md § Hook Pipeline): interrupted → "interrupted",
    completed → "ok", else → "error". The interrupted-path fire sites
    (tui_gateway/session_lifecycle.py:386-392, cli.py:869-874) may omit
    ``failed``/``turn_exit_reason``; both are optional here.
    """
    out: Dict[str, Any] = {}
    _put(out, "session_id", kwargs.get("session_id"))
    _put(out, "model", kwargs.get("model"))
    _put(out, "platform", kwargs.get("platform"))
    _put(out, "turn_id", kwargs.get("turn_id"))
    _put(out, "turn_exit_reason", kwargs.get("turn_exit_reason"))
    completed = kwargs.get("completed")
    interrupted = kwargs.get("interrupted")
    if interrupted:
        out["run_status"] = "interrupted"
    elif completed:
        out["run_status"] = "ok"
    else:
        out["run_status"] = "error"
    return out


# ---------------------------------------------------------------------------
# Delegation (subagent) metrics
# ---------------------------------------------------------------------------


def delegation_metrics_from_subagent_start(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """subagent_start kwargs (tools/delegate_tool.py:287-295) → delegation edge."""
    out: Dict[str, Any] = {}
    _put(out, "parent_session_id", kwargs.get("parent_session_id"))
    _put(out, "parent_turn_id", kwargs.get("parent_turn_id"))
    _put(out, "parent_subagent_id", kwargs.get("parent_subagent_id"))
    _put(out, "child_session_id", kwargs.get("child_session_id"))
    _put(out, "child_subagent_id", kwargs.get("child_subagent_id"))
    _put(out, "child_role", kwargs.get("child_role"))
    _put(out, "child_goal", kwargs.get("child_goal"))
    return out


def delegation_metrics_from_subagent_stop(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """subagent_stop kwargs (tools/delegate_tool_results.py:355-370) → delegation close.

    child_status canonical values per tools/delegate_tool.py::_run_single_child
    contract: completed | interrupted | failed (duration from
    entry.duration_seconds → duration_ms by the fire site).
    """
    out: Dict[str, Any] = {}
    _put(out, "parent_session_id", kwargs.get("parent_session_id"))
    _put(out, "child_session_id", kwargs.get("child_session_id"))
    _put(out, "child_role", kwargs.get("child_role"))
    _put(out, "child_status", kwargs.get("child_status"))
    _put(out, "child_summary", kwargs.get("child_summary"))
    _put(out, "duration_ms", _int(kwargs.get("duration_ms")))
    return out


# ---------------------------------------------------------------------------
# Kanban dispatch-level metrics (worker lifecycle observers)
# ---------------------------------------------------------------------------


def dispatch_metrics_from_kanban_worker_spawned(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """on_kanban_worker_spawned kwargs (hermes_cli/kanban_db.py:193-207) → spawn metrics."""
    out: Dict[str, Any] = {}
    _put(out, "task_id", kwargs.get("task_id"))
    _put(out, "board", kwargs.get("board"))
    _put(out, "run_id", _int(kwargs.get("run_id")))
    _put(out, "worker_pid", _int(kwargs.get("worker_pid")))
    _put(out, "workspace_path", kwargs.get("workspace_path"))
    _put(out, "profile/role", kwargs.get("profile_name"))
    return out


def dispatch_metrics_from_kanban_worker_exited(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """on_kanban_worker_exited kwargs (hermes_cli/kanban_db_dispatch.py:1156-1164,1270-1295).

    exit_kind ∈ clean_exit | rate_limited | nonzero_exit | signaled | unknown;
    outcome is the run outcome recorded by the reclaim.
    """
    out: Dict[str, Any] = {}
    _put(out, "task_id", kwargs.get("task_id"))
    _put(out, "board", kwargs.get("board"))
    _put(out, "run_id", _int(kwargs.get("run_id")))
    _put(out, "worker_pid", _int(kwargs.get("worker_pid")))
    _put(out, "exit_kind", kwargs.get("exit_kind"))
    _put(out, "exit_code", _int(kwargs.get("exit_code")))
    _put(out, "outcome", kwargs.get("outcome"))
    _put(out, "retry_status", kwargs.get("retry_status"))
    _put(out, "profile/role", kwargs.get("profile_name"))
    return out


def dispatch_metrics_from_kanban_worker_stale_claim(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """on_kanban_worker_stale_claim kwargs (hermes_cli/kanban_db.py:2501-2505)."""
    out: Dict[str, Any] = {}
    _put(out, "task_id", kwargs.get("task_id"))
    _put(out, "board", kwargs.get("board"))
    _put(out, "run_id", _int(kwargs.get("run_id")))
    _put(out, "worker_pid", _int(kwargs.get("worker_pid")))
    if kwargs.get("heartbeat_stale") is not None:
        out["heartbeat_stale"] = bool(kwargs.get("heartbeat_stale"))
    _put(out, "retry_status", kwargs.get("retry_status"))
    _put(out, "profile/role", kwargs.get("profile_name"))
    return out


def dispatch_metrics_from_kanban_task_updated(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """on_kanban_task_updated kwargs (hermes_cli/kanban_db.py:209-224).

    ``changed_fields`` carries field NAMES only, never values (core contract).
    """
    out: Dict[str, Any] = {}
    _put(out, "task_id", kwargs.get("task_id"))
    _put(out, "board", kwargs.get("board"))
    _put(out, "run_id", _int(kwargs.get("run_id")))
    changed = kwargs.get("changed_fields")
    if isinstance(changed, (list, tuple)):
        out["changed_fields"] = [str(f) for f in changed]
    _put(out, "profile/role", kwargs.get("profile_name"))
    return out
