# Utilization-controller metrics → R-OBS-00 correlation envelope — field-by-field mapping (HTB-B2)

- **Schema:** `correlation-envelope.schema.json` v1.0.0 (`urn:sovav-hermes:telemetry:correlation-envelope:1.0.0`,
  sha256 `5271d5fa…33e70a`) — READ-ONLY reference at
  `C:/tmp/sovav-hermes/r-obs-00/docs/telemetry/`.
- **Contract doc:** `telemetry-contract.md` v1.0.0 (sha256 `599e38f3…b657`) — same READ-ONLY reference.
- **Evidence base:** the INSTALLED v0.21.3 Hermes runtime at
  `C:/Users/Sovav/AppData/Local/hermes/hermes-agent` — commit `0ddba07ad7`, `__version__ = "0.21.3"`
  (`hermes_cli/__init__.py:6`). All `file:line` citations below are against that tree. Upstream `main`
  is reference-only; the installed runtime is authoritative for this plugin.
- **Extraction module:** `metrics.py` (repo root) — pure functions, hook kwargs in → metric dict out.
  The `GAPS` list in `metrics.py` is asserted by `tests/test_metrics.py` so it cannot rot silently.

Scope note: the envelope schema declares `additionalProperties: false` — token counts, active time,
tool metrics etc. **cannot ride on the envelope**. The controller consumes them as event payloads
joined by the envelope's `task_id + run_id` / `trace_id + correlation_id` keys (contract §1.4). The
tables below therefore cover (A) envelope base fields, (B) Turnstone extension + harness propagation
fields, and (C) the controller-consumed metric set beyond the envelope.

---

## A. Envelope base fields (contract §1.1 — exactly twelve)

| # | Envelope field | Metric meaning | Source hook(s) + exact kwarg path | Sourceable from v0.21.3 hooks? | Gap note |
|---|----------------|----------------|-----------------------------------|:---:|----------|
| 1 | `board` | Board slug of the originating card | `kanban_task_claimed` / `kanban_task_completed` / `kanban_task_blocked` → `board` (`hermes_cli/kanban_db.py:165-172`, fire sites `:2288, :2803, :3172-3174`); also `on_kanban_worker_*` and `on_kanban_dispatch_tick` → `board` (`kanban_db.py:193-258`) | **Y** (kanban-lifecycle events only) | Not present on LLM/tool hook kwargs; the plugin joins LLM-call events to the claim via profile + session/run correlation. |
| 2 | `task_id` | Board task id of the card being worked | `kanban_task_*` → `task_id` (`kanban_db.py:157,165-172,2288,2803,3172-3174`) | **Y** (kanban-lifecycle events only) | **Trap:** the `task_id` kwarg on `pre_llm_call`/`post_api_request`/`post_tool_call` is a per-turn identity (`effective_task_id = task_id or str(uuid.uuid4())`, `agent/turn_context.py:489`, `agent/turn_facade.py:56`) — NOT the kanban card id. Do not map hook `task_id` → envelope `task_id`. |
| 3 | `run_id` | Dispatch/run id of the emitting run | `kanban_task_*` → `run_id` (`kanban_db.py:165-172`); `on_kanban_worker_spawned/exited/stale_claim` → `run_id` (`kanban_db.py:199-205,2501-2505`; payload built `kanban_db_dispatch.py:1156-1164`) | **Y** (kanban-lifecycle events only) | Schema type integer-or-string; kanban hooks carry an int. Same join caveat as `task_id` for LLM events. |
| 4 | `parent_task_ids` | Parent card ids of `task_id` | — no hook kwarg | **N** | No kanban lifecycle hook carries the parent set. Needs `kanban_task_claimed` kwarg `parent_task_ids: list[str]`. |
| 5 | `profile/role` | Emitting Hermes profile (= role) | `PluginContext.profile_name` property (`hermes_cli/plugins.py:403-409`, resolves `hermes_cli.profiles.get_active_profile_name()`); verbatim on every kanban hook → `profile_name` (`kanban_db.py:157-158`) | **Y** | Multiplexed-gateway accuracy caveat inherited from `ctx.profile_name` (ONBOARDING.md § Budget Engine → profile attribution). Kanban hooks resolve the profile independently (`_hook_profile_name`, `kanban_db.py:174-181`) — the stronger source. |
| 6 | `model` | Provider-qualified model id as invoked | `on_session_start.model` (`agent/conversation_loop.py:773-778`); `pre_llm_call.model` (`agent/turn_context.py:693-706`); `post_api_request.model` (`agent/turn_response_intake.py:62-91`); actual served model: `post_api_request.response.model` / `response_model` (`agent/api_request_hooks.py:142-173`) | **Y** | — |
| 7 | `provider` | Model provider id | `post_api_request.provider` (verbatim, `agent/turn_response_intake.py:71`); also `pre_api_request.provider` (`agent/turn_api_request.py:71`) and `api_request_error.provider` (`agent/api_request_hooks.py:197`) | **Y** | Verbatim-from-gateway convention stands (ONBOARDING.md); the actually-serving upstream is separately visible as `post_api_request.response.provider` (`api_request_hooks.py:148-163`). |
| 8 | `key_id` | NON-SECRET credential label | — no hook kwarg | **N** | Hook payloads sanitize credential *values* to `<redacted>` (`api_request_hooks.py:60-64,95`) but expose no key name/hash prefix. Needs a `key_id` (label-only) kwarg on `post_api_request`, sourced from the gateway's credential layer. |
| 9 | `call_class` | Call path class | Partial: `cron` derivable from `platform == "cron"` (`on_session_start`/`on_session_end` platform kwarg) or the session-id prefix (`cron_{job_id}_{YYYYMMDD_HHMMSS}`, `cron/scheduler.py:2302`); `delegate` derivable from non-empty `pre_llm_call.parent_session_id` (`agent/turn_context.py:703`) | **Partial** (2 of 6) | `fallback`: no flag; only indirect evidence via `response.provider` ≠ requested provider. `aux`: auxiliary calls (`agent/auxiliary_client.py::call_llm`) fire NO hooks — no kwarg exists. `judge`: no hook covers the goal-mode judge path (`VALID_HOOKS`, `hermes_cli/plugins.py:108-200`). `metrics.call_class_from_session_context` returns only `cron`/`delegate`/`None` and never guesses. |
| 10 | `handoff_id` | Handoff/transition identifier | — no hook kwarg | **N** | `kanban_task_completed` carries a free-text `summary` (`kanban_db.py:2803`), not an id. Needs `handoff_id` kwarg on completed/blocked. |
| 11 | `worktree/branch` | Git branch of the isolated worktree | — no hook kwarg | **N** | `on_kanban_worker_spawned.workspace_path` is a filesystem path, not a branch (`kanban_db.py:193-207`; privacy-noted `kanban_db_dispatch.py:1289-1290`). Needs a `branch` kwarg on claimed/worker-spawned. |
| 12 | `trace_id` | Cross-repo trace identifier | — no hook kwarg | **N** | `pre_api_request.middleware_trace` (`agent/turn_api_request.py:79`) is the LLM request-middleware trace, not a trace id. Needs `trace_id` kwarg on `post_api_request` (or a documented LangSmith run-id bridge). |

## B. Turnstone extension fields + harness propagation (contract §1.2/§1.5)

| Field | Sourceable from v0.21.3 hooks? | Note |
|-------|:---:|------|
| `repo`, `agent_run_id`, `correlation_id`, `idempotency_key`, `deal_id`, `worker_id`, `model_ref`, `trigger_type`, `runtime_environment`, `basis_fingerprint`, `worker_definition_version`, `output_schema_version` | n/a | Per schema/contract these MUST be `null` (or absent) on harness-originated events. No harness hook may ever populate them — extraction emits nothing for these twelve. |
| `harness_task_id` / `harness_run_id` | n/a | Populated on the *Turnstone* side from `TURNSTONE_HARNESS_TASK_ID` / `TURNSTONE_HARNESS_RUN_ID` (contract §1.5). The harness side has no hook that carries them; pair-consistency is enforced by the schema, not by extraction. |

## C. Controller metrics beyond the envelope (contract §3.1 names "cost/token aggregates, timing/latency, counts, ids, error codes" as the metadata-only export class)

| Metric | Source hook(s) + exact kwarg path | Sourceable? | Notes / gap |
|---------|-----------------------------------|:---:|-------------|
| Token: input (non-cached) | `post_api_request.usage.input_tokens` (`agent/turn_response_intake.py:88`; shape `agent/api_request_hooks.py:35-49` over `CanonicalUsage`, `agent/usage_pricing.py:56-71`) | **Y** (real) | `usage` is `None` when the provider reports nothing — no token keys emitted; estimation is the caller's policy (GAPS). |
| Token: output | `post_api_request.usage.output_tokens` | **Y** (real) | — |
| Token: reasoning | `post_api_request.usage.reasoning_tokens` | **Y** (real) | — |
| Token: cache read / cache write | `post_api_request.usage.cache_read_tokens` / `.cache_write_tokens` | **Y** (real) | **Not a gap** — both buckets are exposed per request; `prompt_tokens = input + read + write` and `total_tokens = prompt + output` come as derived properties (`usage_pricing.py:65-71`). Never sum `prompt_tokens` with its components (double-count; ONBOARDING.md). |
| Token: request count | `post_api_request.usage.request_count` | **Y** | — |
| Active time (per request) | `post_api_request.api_duration` (seconds; `agent/turn_response_intake.py:75`) + `started_at`/`ended_at` (`:76-77`) | **Y** | `metrics.py` converts to `api_duration_ms`. |
| Time-to-first-token | `post_api_request.first_chunk_at` − `started_at` (`agent/turn_response_intake.py:79-81`) | **Y** (when streamed) | `None` when not streamed. |
| Active time (cumulative) | — no hook kwarg | **N** | Lives in `agent/activity_tracking.py` (`_touch_activity` liveness clock) and bridges internally to the kanban heartbeat; not exposed to plugins. Needs `on_session_end` kwarg `active_seconds`. |
| Provider-billed cost | `post_api_request.response.billed_cost_usd` (`agent/api_request_hooks.py:148-163`) | **Y** (when provider reports) | OpenRouter reports `usage.cost`; other providers leave it `None`. Plugin-estimated cost stays `pricing.py`'s job (out of extraction scope). |
| Provider response id | `post_api_request.response.id` (`api_request_hooks.py:161`) | **Y** | Gateway/provider generation id — useful dedupe key. |
| Serving upstream | `post_api_request.response.provider` (`api_request_hooks.py:162`) | **Y** | The upstream that actually served (OpenRouter routing field). |
| Request ids | `turn_id` + `api_request_id` on `pre_api_request` / `post_api_request` / `post_tool_call` / `api_request_error` (`agent/turn_api_request.py:63-64`, `turn_response_intake.py:64-65`, `model_tools.py:692-693`, `api_request_hooks.py:189-191`) | **Y** | — |
| Retry context | `pre_api_request.retry_count` (`turn_api_request.py:73`); `api_request_error.retry_count/max_retries/retryable/reason/status_code` (`api_request_hooks.py:203-208`) | **Y** | — |
| Tool call metrics | `post_tool_call`: `tool_name, args, result, task_id, session_id, tool_call_id, turn_id, api_request_id, duration_ms, status, error_type, error_message, middleware_trace` (`model_tools.py:676-695`; `_CallIds` at `:643-653`) | **Y** | `status` is derived by core (`_tool_result_observer_fields`, `model_tools.py:656-671`) — the plugin no longer needs its own JSON-error heuristic for the hook path. |
| Run outcome | `on_session_end`: `session_id, task_id, turn_id, completed, failed, interrupted, turn_exit_reason, model, platform` (`agent/turn_finalizer.py:659-671`); interrupted-path variants: `tui_gateway/session_lifecycle.py:386-392`, `cli.py:869-874` | **Y** | Status derivation `interrupted → "interrupted"`, `completed → "ok"`, else `"error"` (plugin convention, ONBOARDING.md). |
| Delegation edges | `subagent_start`: `parent_session_id, parent_turn_id, parent_subagent_id, child_session_id, child_subagent_id, child_role, child_goal` (`tools/delegate_tool.py:287-295`); `subagent_stop`: + `child_summary, child_status, tool_call_history, duration_ms` (`tools/delegate_tool_results.py:349-372`) | **Y** | `child_status` ∈ completed/interrupted/failed (`tools/delegate_tool.py` `_run_single_child` contract). Still no token/cost data on the hook itself; child cost arrives via the child's own hooks. |
| MoA reference (advisor) metrics | `post_api_request.moa_references` (`agent/turn_response_intake.py:88`; `agent/conversation_loop.py:270-280`) | **Y** (new in 0.21.3) | Per-slot advisor spend attached to the aggregator generation. **Drift vs ONBOARDING.md** § MoA ("reference tokens never captured"): the hook now carries `moa_references`; the one-shot `/moa` path still fires no hooks at all. |
| Dispatch-level controller metrics | `on_kanban_worker_spawned`: `task_id, board, assignee, run_id, worker_pid, workspace_path` (`kanban_db.py:193-207`); `on_kanban_worker_exited`: `exit_kind` (clean_exit/rate_limited/nonzero_exit/signaled/unknown), `exit_code`, `outcome`, `retry_status` (`kanban_db_dispatch.py:1156-1164,1270-1295`); `on_kanban_worker_stale_claim`: `heartbeat_stale`, `retry_status` (`kanban_db.py:2501-2505`); `on_kanban_task_updated`: `changed_fields` (names only, `kanban_db.py:209-224`); `on_kanban_dispatch_tick`: `board, profile_name, dry_run, outcome, result: DispatchResult` (`kanban_db.py:228-258`) | **Y** (new in 0.21.3) | These cover the harness "dispatch stall" alert class inputs (contract §5.1) directly from hooks. `workspace_path` is privacy-sensitive — redaction is B1's concern; extraction passes it through but the mapping flags it. |
| Skill usage | `on_skill_lifecycle`: `action, skill_name, provenance, task_id, session_id, use_count, reused, reuse_after_patch` (`tools/skill_usage.py:455-464`) | **Y** | Available but not named controller-consumable by the contract — extracted only if the controller asks. |
| Cron job id | Parsed from `session_id` (`cron_{job_id}_{YYYYMMDD_HHMMSS}`, `cron/scheduler.py:2302`) via `metrics.cron_job_id_from_session_id` (anchored parse) | **Y** (parsed) | No `cron_job_id` kwarg exists in any hook (ONBOARDING.md § Cron Job Identification). |
| Review metrics | `claim_review_task` (`hermes_cli/kanban_db.py:2292`) fires NO lifecycle hook | **N** | Review-lane claims and verdicts (approve / request_changes) are invisible; only the eventual `kanban_task_completed.summary` is observable. Needs a review-lane claim/verdict hook. |
| Artifact metrics | — no hook | **N** | Hermes has no artifact store; artifacts are transcript references (ONBOARDING.md § Hermes Session Storage). Needs a post_tool_call artifact-path enrichment. |
| Tokens when `usage is None` | `pre_api_request.approx_input_tokens` (`agent/turn_api_request.py:80`) | **Partial** | Character-based estimate only; flagged `estimated` by the caller. Estimation policy is not extraction. |

---

## Explicit GAPS list (mirrors `metrics.GAPS`, asserted by tests)

1. **`envelope.key_id`** — no credential label on any hook; values are sanitized to `<redacted>`
   (`api_request_hooks.py:60-64,95`). Needed: label-only `key_id` kwarg on `post_api_request`.
2. **`envelope.parent_task_ids`** — no kanban hook carries the parent set. Needed: `parent_task_ids`
   kwarg on `kanban_task_claimed`.
3. **`envelope.trace_id`** — no hook carries a cross-repo trace id. Needed: `trace_id` kwarg on
   `post_api_request` or a documented LangSmith run-id bridge.
4. **`envelope.handoff_id`** — only a free-text summary on completion. Needed: `handoff_id` kwarg.
5. **`envelope.worktree/branch`** — only a filesystem `workspace_path` on worker spawn. Needed:
   `branch` kwarg on claimed/worker-spawned.
6. **`call_class` coverage (2/6)** — `primary` is a caller default, `cron`/`delegate` are derivable;
   `fallback`/`aux`/`judge` are unsourceable (`aux` structurally: `agent/auxiliary_client.py::call_llm`
   contains no hook dispatch). Needed: `call_class`/`failover_reason` kwarg on `post_api_request`; hook
   dispatch in the auxiliary client; a judge-lifecycle observer.
7. **`active_time.cumulative`** — liveness clock (`agent/activity_tracking.py`) is not hook-exposed.
   Needed: `active_seconds` kwarg on `on_session_end`.
8. **`review.claimed` / `review.decision`** — `claim_review_task` fires no hook (`kanban_db.py:2292`).
   Needed: review-lane claim + verdict observer hooks.
9. **`artifact.metrics`** — no hook; artifacts are transcript references. Needed: tool-result artifact
   enrichment on `post_tool_call`.
10. **Tokens when `usage is None`** — only `approx_input_tokens` estimation input exists; guaranteed
    provider usage reporting is an upstream feature, not a plugin hook.

## Envelope-field summary count

- Base fields: **12** rows — sourceable **6** (`board`, `task_id`, `run_id`, `profile/role`, `model`,
  `provider`), partial **1** (`call_class`: 2 of 6 enum values derivable), not sourceable **5**
  (`parent_task_ids`, `key_id`, `handoff_id`, `worktree/branch`, `trace_id`).
- Turnstone extension + harness fields: **14** rows — n/a (must be null on harness events).

## Verified drift: ONBOARDING.md vs installed v0.21.3 (recorded here; ONBOARDING.md is owned by
another stream — do not silently patch)

1. `VALID_HOOKS` grew substantially (`plugins.py:108-200`): stream observers (`on_stream_start/delta/end`,
   `on_interim_message`), `pre_verify`, `transform_api_error_classification`, `on_skill_lifecycle`,
   `agent_loop_stopped`, the kanban observer family (`kanban_task_claimed/completed/blocked`,
   `on_kanban_worker_spawned/exited/stale_claim`, `on_kanban_task_updated`, `on_kanban_dispatch_tick`),
   `gateway_platform_event`, `pre_transcription`, `pre_command`, `on_room_member_activity`.
2. `on_session_start` now carries exactly `session_id, model, platform` (`conversation_loop.py:773-778`).
3. `on_session_end` gained `task_id, turn_id, failed, turn_exit_reason` (`turn_finalizer.py:659-671`).
4. `post_api_request` gained `turn_id, api_request_id, base_url, api_mode, started_at, ended_at,
   first_chunk_at, message_count, response, moa_references` (`turn_response_intake.py:62-91`).
5. `post_tool_call` gained `tool_call_id, turn_id, api_request_id, status, error_type, error_message,
   middleware_trace` (`model_tools.py:676-695`).
6. `subagent_stop` gained `tool_call_history` (`delegate_tool_results.py:365-370`).
7. **MoA:** `post_api_request.moa_references` now exposes per-advisor metrics on the aggregator
   generation — ONBOARDING.md's "reference tokens are never captured" statement is stale for the
   preset-as-model path; the one-shot `/moa` path still fires no hooks.
8. Profile identity: `PluginContext.profile_name` property exists (`plugins.py:403-409`); kanban hooks
   carry `profile_name` independently.
