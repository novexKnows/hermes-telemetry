# HTC-C1 — sovav-hermes harness-gate compatibility matrix

Plugin **hermes-telemetry 0.8.0** (integrated tip `9a66874`, branch `wt/htc-c1-compat`)
verified against the **sovav-hermes harness gates** on the **installed Hermes Agent v0.21.3**.

Prepared by HTC-C1 (card t_23a64610), 2026-09-19; corrections applied by
HTC-C1-FIX (card t_6e8da224) after the G2 review (t_62e1d587) — see the
Correction record. Harness repo is READ-ONLY; the installed runtime is
READ-ONLY; every dynamic check ran in a disposable home (and every
Kanban-touching probe additionally under full board isolation).

## Ground truth

| Item | Value | Evidence |
|---|---|---|
| Plugin version | 0.8.0 | `plugin.yaml` (`version: "0.8.0"`), 12 hooks declared |
| Integration tip | `9a66874` | `git log --oneline -1` on `wt/htc-c1-compat` after `git merge --ff-only wt/htc-integration` (fast-forward from `d03f3ab`) |
| Installed runtime | Hermes Agent **v0.21.3** | `hermes_cli/__init__.py:6` (`__version__ = "0.21.3"`); driver printed `hermes_cli.__version__=0.21.3 (C:\Users\Sovav\AppData\Local\hermes\hermes-agent\hermes_cli\__init__.py)`. Note (from HTA-A2 review): the installed checkout is dirty at `agent/api_request_hooks.py` + `agent/auxiliary_client.py`; citations describe the live installed state |
| Harness repo | `C:/Users/Sovav/dev/sovav-hermes` @ `41bba4d` | `git log --oneline -1` (read-only). Working tree is DIRTY (4 modified tracked files — `plugin/sovav-roles/dashboard/plugin_api.py`, `plugin/sovav-roles/desktop/plugin.js`, `roles/README.md`, `scripts/setup-profiles.ps1` — plus untracked files). Every dirty mtime predates C1 (2026-09-17T23:30–2026-09-19T00:04; the G2 review's full repo walk found 0 files modified after 13:50 on 2026-09-19). `roles/README.md` line citations below therefore describe the dirty checked-out state |
| Disposable home | `C:\Users\Sovav\AppData\Local\Temp\htc-c1-home` (original runs); `C:\Users\Sovav\AppData\Local\Temp\htc-c1-fix-home` (HTC-C1-FIX re-run) | Fresh dir; plugin files copied from the worktree into `plugins/hermes-telemetry/`; `config.yaml` = `plugins.enabled: [hermes-telemetry]` |
| Env pinning | THREE-PIN ISOLATION (operator ruling): `HERMES_HOME`, `HERMES_TELEMETRY_HOME`, AND `HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD` all pointed at the disposable location in every Kanban-touching run | Driver logs (pin lines of each run). `paths.py:32` gives `HERMES_TELEMETRY_HOME` priority (shared cost-center dir) — the A2 hygiene incident cause; pinning both is mandatory. Two-pins-only is NOT enough: C1-B1 (see Correction record) happened because the original gate-2 driver left the Kanban pins inherited and mutated the live board. Mechanism note: the runtime's own fence (`hermes_cli/kanban_db.py:147` → `agent/delegation_context.py:121-142`) is not a reliable backstop — it only denies a worker's dispatcher-pinned board path, and it treats ANY path equal to the `HERMES_KANBAN_DB` pin as fenced even when that pin is disposable, so a correctly-pinned probe must clear the inherited `HERMES_DELEGATED_CHILD_CONTEXT` marker after redirecting every Kanban surface |
| Real-home leak check | board DB zero delta + `c1_* rows=0` (covers BOTH `telemetry.db` and the board DB) | Original drivers: two logs exist and only driver 2 carried a leak-check line — `[leak-check] real home telemetry.db: runs=377 llm_calls=12045 c1_gate rows=0` (`llm_calls` grows only from the live gateway's own sessions; zero rows carry a `c1_gate` session id). HTC-C1-FIX re-run adds the mandated board-DB check: live board row counts identical before/after the probe (tasks=54, task_events=1377, task_runs=52, task_links=42, task_comments=121, task_attachments=30, kanban_notify_subs=0) and live `telemetry.db` counts unchanged with `c1_fix/c1_gate rows=0` — `htc-c1-fix-driver.log` `[pre]`/`[post]` lines |

Driver logs (disposable, absolute paths):
`C:\Users\Sovav\AppData\Local\Temp\htc-c1-gate-driver.log` (part 1),
`C:\Users\Sovav\AppData\Local\Temp\htc-c1-gate-driver2.log` (part 2),
`C:\Users\Sovav\AppData\Local\Temp\htc-c1-fix-driver.log` (HTC-C1-FIX re-run —
three-pin isolation, board-DB leak check, corrected pricing lookup).

## Registration snapshot (dynamic, real runtime dispatch)

All 12 declared hooks registered with exactly 1 callback each — `api_request_error`,
`on_session_end`, `on_session_finalize`, `on_session_start`, `post_api_request`,
`post_llm_call`, `post_tool_call`, `pre_api_request`, `pre_llm_call`, `pre_tool_call`,
`subagent_start`, `subagent_stop`.

Gate-path hooks **not** registered (observer surfaces left untouched — 0 callbacks,
verified live): `on_skill_lifecycle`, `pre_gateway_dispatch`, `agent_loop_stopped`,
`pre_approval_request`, `post_approval_response`, `kanban_task_claimed`,
`on_kanban_worker_spawned`, `on_kanban_dispatch_tick`,
`transform_api_error_classification`.

## The 8-gate matrix

Verdict key: **y** = compatible (plugin neither breaks the gate nor requires a
harness-side change), with any observability gaps listed. Known cross-repo gaps
(G1–G7, HARNESS-A) are referenced from the TM-A audit t_c8fca739, not re-derived.

### 1. context/checkpoint gate — y (compatible; two known cost-accuracy gaps)

- Static: the plugin owns the full conversation-loop surface behind context
  management — `on_session_start/end/finalize`, `pre/post_llm_call`,
  `pre/post_api_request` (all registered). Runtime compression triggers on
  `compression.threshold_tokens`; the harness installed **204000** for operator
  and reviewer (live profile configs `profiles/operator/config.yaml:67`,
  `profiles/reviewer/config.yaml:68` — HARNESS-C criterion 1).
- Dynamic (real dispatch, disposable home): `on_session_start` → `post_api_request`
  (usage incl. `reasoning_tokens: 150`) → `on_session_end` → `on_session_finalize`
  produced a real runs row:
  `{"session_id": "c1_gate_sess_001", "status": "ok", "tokens_in": 5000, "tokens_out": 300, "cost_usd": 0.000585, ...}`.
- Known cross-repo gaps intersecting this gate:
  - **G1** — compression/summarization/micro-compaction spend is auxiliary
    `call_llm` and fires **no hooks** (`agent/auxiliary_client.py` contains zero
    `invoke_hook`/`has_hook` references — verified by grep this run). The
    checkpoint/compression LLM spend that the harness's token-efficiency controls
    drive is invisible to telemetry. BLOCKING for "fully married" (routed to PM).
  - **HARNESS-A** — reasoning-token double-charge reproduced live in the disposable
    fix home (`htc-c1-fix-home`): rates read with the CORRECTED lookup —
    `models["z-ai/glm-5.3-flash"]` in the disposable `telemetry/pricing.yaml`
    (seeded from the prior DISPOSABLE home, never the real one) →
    `{input: 0.09, output: 0.3, _source: openrouter}`; captured row 5000 in /
    300 out / 150 reasoning → recorded `cost_usd = 0.000585` = double-charge
    formula `(5000*0.09 + 300*0.3 + 150*0.3)/1e6`; correct excess-only formula
    gives `0.000540` — recorded value, both rates, and both formula results all
    printed by the same run (`htc-c1-fix-driver.log [pricing]` lines).
    Provenance: the original driver's rate finder keyed on the bare model name,
    missed the `models` map and printed `pricing.yaml rates for
    glm-5.3-flash: null`, so its cost-comparison line never ran (review
    finding C1-M3); the numbers above now come from output produced by the
    corrected re-run. Fix `61bc2c9` is not in fork main (t_c8fca739 verdict).
- Required change (this repo): none for compatibility. G1 + HARNESS-A are
  cross-repo dependencies (see final section).

### 2. dispatch gate (inbound dispatch to workers) — y

- Static: the plugin registers none of the dispatch-path hooks
  (`kanban_task_claimed` fire site `hermes_cli/kanban_db.py:2288`;
  `on_kanban_worker_spawned` `hermes_cli/kanban_db.py:195-200`, fired after PID
  persist; `on_kanban_dispatch_tick` post-lock per `plugins.py:108-199`; gateway
  inbound `pre_gateway_dispatch` `gateway/run_inbound.py:66-89,234`). With zero
  registered callbacks the plugin structurally cannot alter dispatch ordering,
  WIP caps, or claim locking.
- Dynamic (re-run under THREE-PIN isolation after C1-B1): real
  `kanban_db_dispatch.dispatch_once(con, board=board, dry_run=True)` run
  **in-process with the plugin loaded** on the genuinely disposable board
  `htc-fix-probe` at
  `C:\Users\Sovav\AppData\Local\Temp\htc-c1-fix-home\kanban\boards\htc-fix-probe\kanban.db`:
  empty-board tick returned all-zero counters (`spawned=[], reclaimed=0,
  skipped_locked=False, ...`); after creating fixture task `t_ec751938`
  (assignee worker, no workspace) the next tick correctly classified it
  `skipped_nonspawnable: ['t_ec751938']` — the dispatch decision path is intact.
  Isolation proof (`htc-c1-fix-driver.log`): the resolved board DB path is
  printed and asserted under the disposable home; the fixture row
  `('t_ec751938', 'c1 fix gate fixture', 'worker', 'ready')` exists ONLY in
  the disposable board (the live board holds no such row); live board row
  counts are identical before/after the probe (zero delta across all 7
  tables) and live `telemetry.db` counts are unchanged with 0 `c1_fix`/
  `c1_gate` rows.
  - **Incident note (C1-B1, blocking, from review t_62e1d587):** the ORIGINAL
    gate-2 evidence at e696bc5 was invalid. Its driver
    (`htc-c1-driver2.py`) pinned `HERMES_HOME` + `HERMES_TELEMETRY_HOME` but
    left `HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD` inherited, so its
    "disposable board `c1-compat-probe`" tick actually ran against the LIVE
    hermes-telemetry board — which held ~49 live program cards at the time,
    not an empty board — and created fixture task `t_db9e21e0` there at
    2026-09-19T14:18:22; the live dispatcher later claimed and executed that
    leaked card as run 50 (session 20260919_144106_27b52c). The fix re-run
    above replaces that evidence; cleanup of `t_db9e21e0` is routed to the
    operator (Correction record below).
- Gap: **G5** — no dispatch/interrupt observability rows exist because
  `pre_gateway_dispatch` + `agent_loop_stopped` are unregistered (TM-A). Adoption
  is optional future work; not required for compatibility.
- Required change: none.

### 3. skill loading gate — y

- Static: `on_skill_lifecycle` is in v0.21.3 `VALID_HOOKS` (`plugins.py:129`) and
  fires at `tools/skill_usage.py:459-460` behind a `has_hook(...)` short-circuit —
  an unregistered plugin costs zero overhead on the skill path.
- Dynamic: `invoke_hook("on_skill_lifecycle", skill="hermes-agent", action="loaded", ...)`
  through the real dispatcher with the plugin loaded → `[]` (no callback, no error).
- Gap: skills-hub auxiliary lookups route to the auxiliary model
  (`roles/README.md:13,25`) → spend invisible (**G1**).
- Required change: none.

### 4. worktree provisioning gate — y

- Static: worktree provisioning is dispatcher/worker-side (`on_kanban_worker_spawned`
  even carries `workspace_path` as a privacy-noted kwarg, `plugins.py:108-199`).
  The plugin never invokes git and registers none of these hooks. The harness's
  approval deny floors on `worktree remove/prune` (`roles/README.md:38`) cannot
  be pre-empted by the plugin: its only veto is the budget gate (see gate 7),
  which is fail-open with no `budget.yaml`.
- Dynamic: `post_tool_call` dispatched through the real dispatcher with
  `tool_name="terminal"`, args a fixture git worktree-add command line
  (`{"command": "<fixture worktree-add invocation>"}`)
  → real tool_calls row `{"tool_name": "terminal", "ok": 1}`; the args string
  (which contains repo layout) is **never emitted** (telemetry-contract §3.2,
  redaction allowlist) — confirmed in the DB readback.
- Required change: none.

### 5. artifact attachment gate — y

- Dynamic: `pre_tool_call` + `post_tool_call` dispatched through the real runtime
  with `tool_name="kanban_attach"` → real tool_calls row
  (tool_name kanban_attach, ok=1); `pre_tool_call` returned `[]` — no veto
  (invoke_hook returns a list; with no `budget.yaml` in the disposable home
  the budget gate is fail-open), so attachments are unimpeded.
- Runtime cap note: `KANBAN_ATTACHMENT_MAX_BYTES = 25 MB`
  (`hermes_cli/kanban_db.py:132`) — plugin-independent, unaffected.
- Required change: none.

### 6. wake gate — y

- Static: the wake path is `gateway/wake.deliver_wake` (call sites
  `gateway/kanban_watchers_notifier.py:593,601,631`;
  `gateway/run_notifications.py:1066-1087`); mid-run interrupts fire
  `agent_loop_stopped` at `gateway/run_agent_cache.py:493-500`. The plugin
  registers neither hook → no interference with wake delivery.
- Dynamic: `invoke_hook("agent_loop_stopped", session_key=..., platform="cli",
  reason="test-stop", ...)` with the plugin loaded → `[]` (no error).
- Coverage: a woken session runs a normal conversation turn, so its LLM/tool
  spend is fully captured by the registered conversation-loop hooks; only
  auxiliary spend inside wake handling is invisible (**G1**).
- Required change: none.

### 7. permission gate (approvals) — y (one scoped veto, by design)

- Static: approval observers are returns-ignored by contract ("plugins cannot
  veto or pre-answer (use pre_tool_call)" — `plugins.py:108-199`; fire sites
  `tools/approval.py:914,917`, `tools/approval_gateway_wait.py:97,110,198`).
  The plugin registers neither. Its **only** veto surface is the budget hard-gate
  in `pre_tool_call` (plugin `__init__.py:947-973`; runtime veto semantics
  `plugins.py:1806-1850`).
- Dynamic (three-point proof, real dispatch):
  1. `invoke_hook("pre_approval_request", command="git push", ..., surface="cli")`
     → `[]` — observer surface untouched.
  2. `pre_tool_call` with **no** `budget.yaml` → `[]` — no veto (fail-open; per
     `budget.example.yaml:4` "If this file is absent, budgets are disabled and
     nothing is enforced") — approvals/allowlists are the only gate.
  3. With `budgets.global.daily_usd: 0.000001` and spend $0.000585 →
     `pre_tool_call` returned
     `[{'action': 'block', 'message': '[BUDGET] Global is over its daily budget: spent $0.0006 of $0.00 (58500%). Tool calls are blocked until the daily window resets. ...'}]`
     — the veto is strictly budget-gated and can never compete with the
     harness's approval allowlists.
- Live harness floors verified read-only: worker profile
  `config.yaml:228 single_query_mode: deny`, `:243 command_allowlist:` — the
  plugin touches neither.
- Required change: none.

### 8. key gates (API key / credential handling surface) — y

- Static: the plugin never reads, stores, or emits provider keys. Its event
  pipeline is key-allowlist based (`redaction.py` `ALLOWED_EVENT_KEYS` — prompt
  text, tool args/results, secrets are DROPPED) with value-level secret-shape
  scrubbing as defense in depth (private-key PEM and credential-URL patterns,
  `redaction.py:163-164`; Unicode default-ignorable stripping `:133`). DB rows
  carry only model/provider/token/cost facts.
- Dynamic:
  - `sanitize({"note": "<fixture: credential-shaped string> rotated"})` →
    `key leaked=False`, scrubbed payload `{}` (the allowlist dropped the field
    before the shape scrubber was even needed) — no key material reaches the
    durable store.
  - `api_request_error` dispatched with the **v0.21.3 kwarg shape**
    (`error={...}, request={...}`, not the plugin's legacy `api_kwargs` path) →
    no crash; the plugin survives via model/provider fallback (**G7** drift,
    confirmed live).
- Gaps: **G4** — `transform_api_error_classification` unregistered, so
  failover/credential-rotation decisions are unrecorded (resulting spend is
  still covered); **HARNESS-A** cost-accuracy above; **G3** failed-attempt cost
  rows. HERMES_TELEMETRY_HOME precedence (`paths.py:32`) is the mechanism that
  leaked synthetic rows into the real home in A2 — it is also the feature that
  supports the harness's per-role cost-center attribution; the operational rule
  "override ALL THREE env-pin groups in Kanban-touching disposable runs"
  (`HERMES_HOME`, `HERMES_TELEMETRY_HOME`, `HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD`)
  is recorded in Ground truth.
- Required change: none for compatibility.

## Verdict summary

| # | Gate | Compatible | Observability gaps (cross-repo refs) | Required change |
|---|---|---|---|---|
| 1 | context/checkpoint | y | G1 (aux/compression spend), HARNESS-A (reasoning double-charge, reproduced live: 0.000585 vs 0.000540) | none |
| 2 | dispatch | y | G5 (no dispatch rows) | none |
| 3 | skill loading | y | G1 (skills-hub aux spend) | none |
| 4 | worktree provisioning | y | — | none |
| 5 | artifact attachment | y | — | none |
| 6 | wake | y | G1 (aux spend in wake handling) | none |
| 7 | permission | y | — | none |
| 8 | keys | y | G4 (rotation decisions), G7 (kwarg drift survived), G3 (failed-attempt rows), HARNESS-A | none |

**Bottom line:** the adapted plugin is compatible with all 8 sovav-hermes harness
gates on installed v0.21.3 — no gate is broken and no harness-side change is
required by this repo. The residual exposure is cost-accuracy/observability, all
of it owned by the already-routed cross-repo gaps below.

## Cross-repo dependency notes (for PM — no work done here, by scope)

1. **G1 (BLOCKING for "fully married")** — all auxiliary `call_llm` spend
   (compression/summarization/micro-compaction, titles, MoA internals, vision,
   MCP sampling, smart approvals, TTS, goal judge, kanban decomposer, plugin LLM)
   fires no hooks; `agent/auxiliary_client.py` has zero hook references in
   v0.21.3. Emitter placement was proposed by TM-A (stream B).
2. **HARNESS-A** — reasoning-token double-charge fix (`61bc2c9`, branch
   `origin/harness/token-efficiency` only) still not in the plugin line; live
   reproduction numbers in gate 1/8 rows above. This repo's `pricing.py` still
   double-charges (t_c8fca739 verdict; gate-8 intersection documented, fix owned
   by the HARNESS-A routing).
3. **G5** — `pre_gateway_dispatch` + `agent_loop_stopped` unregistered: the
   harness's gateway-driven dispatch/wake flow produces no telemetry rows.
   Optional adoption, decision for PM/TM.
4. **G4** — `transform_api_error_classification` unregistered: OpenRouter
   per-role key rotation/failover events are unrecorded, weakening the audit
   trail around the harness's key gates.
5. **G7/G3/G2** — `api_request_error` kwarg drift (survives via fallback),
   failed-attempt cost rows, dropped `moa_references` metrics (per-advisor
   observability the harness's MoA usage would want).
6. **Operator cleanup still pending from A2** (not this card's scope): the two
   synthetic rows (`session_id='proof_sess_001'`, `llm_calls.id=5947`) in the
   real `telemetry.db` await the operator waiver/cleanup decision recorded on
   t_c8fca739. This run added zero rows to the real home (leak check above).
7. **Operator cleanup pending from C1-B1** (not this card's scope): the leaked
   fixture card `t_db9e21e0` on the live hermes-telemetry board (created
   2026-09-19T14:18:22 by the original gate-2 driver, executed by the live
   dispatcher as run 50) awaits the operator waiver/cleanup decision. Not
   deleted or modified by the plugin work — operator-owned (Correction record).

## Correction record (HTC-C1-FIX, card t_6e8da224)

Applied after the G2 different-family review (t_62e1d587) returned
CHANGES_REQUESTED on e696bc5 — 1 blocking, 4 minor findings, all addressed
docs-only (no code, no harness-side change).

**C1-B1 (blocking) — gate-2 evidence integrity.** The original gate-2 probe
driver (`htc-c1-driver2.py:8-12`) pinned `HERMES_HOME` + `HERMES_TELEMETRY_HOME`
but left the inherited `HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD` pins in place,
so `kanban_db_path("c1-compat-probe")` resolved to the REAL board file:
`C:/Users/Sovav/AppData/Local/hermes/kanban/boards/hermes-telemetry/kanban.db`.
The "empty-board tick" therefore ran against the live program board (~49
cards at the time, NOT an empty board), and fixture task `t_db9e21e0` was
created on the live board at 2026-09-19T14:18:22 (row
`('t_db9e21e0','c1 gate fixture','worker','ready',1789841902)`); the live
dispatcher then claimed and executed it as run 50 (pid 55104, 14:41:04–
14:41:36, worker session `20260919_144106_27b52c`, workspace
`.../boards/hermes-telemetry/workspaces/t_db9e21e0`). The t_23a64610 card
comment claiming the fixture "lives only under the disposable home" was FALSE
and is retracted (retraction posted on that card). Cleanup of `t_db9e21e0` is
routed to the operator — NOT deleted or modified by this work.

Corrective re-run (gate 2, this document): driver
`C:\Users\Sovav\AppData\Local\Temp\htc-c1-fix-driver.py` with full three-pin
isolation — `HERMES_HOME` + `HERMES_TELEMETRY_HOME` +
`HERMES_KANBAN_HOME`/`HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD` (+ workspaces and
attachments roots) all pointed at disposable home
`htc-c1-fix-home`; board `htc-fix-probe`; fixture `t_ec751938` created ONLY
there. Leak check extended to the board DB per the operator ruling: live board
row counts identical before/after across all 7 tables; live `telemetry.db`
unchanged with `c1_fix/c1_gate rows=0` (`htc-c1-fix-driver.log`).

**C1-M1** — two `file:line` citations were off by one: `KANBAN_ATTACHMENT_MAX_BYTES`
is `hermes_cli/kanban_db.py:132` (:131 is `_IS_WINDOWS`); `HERMES_TELEMETRY_HOME`
precedence is `paths.py:32` (:31 is the docstring). Both corrected above.

**C1-M2** — the document said `pre_tool_call` "returned `None`"; `invoke_hook`
returns a LIST, and the actual value in both the original log and the fix run is
`[]`. Reworded to `[] — no veto` (semantics unchanged).

**C1-M3** — the original driver printed `pricing.yaml rates for
glm-5.3-flash: null` (finder missed `models["z-ai/glm-5.3-flash"]`), so the
0.000585-vs-0.000540 comparison was never produced by that run. Re-run with the
corrected lookup in the fix home: rates `{input: 0.09, output: 0.3}` read from
`models["z-ai/glm-5.3-flash"]`, row 5000/300/150 → recorded `cost_usd 0.000585`
(= double-charge formula), excess-only `0.000540` — all from
`htc-c1-fix-driver.log [pricing]` lines produced this run.

**C1-M4** — "constant across all three driver runs" was unsupported (two driver
logs exist; only driver 2 carried a leak-check line) — restated in Ground
truth. The harness-repo ground-truth row now records the dirty working tree
alongside the `41bba4d` HEAD, with mtime evidence that the dirt predates C1.

## Verification performed for this document

- Disposable dynamic: two driver scripts driving the installed runtime's real
  `hermes_cli.plugins.invoke_hook` / `kanban_db_dispatch.dispatch_once` with
  `HERMES_HOME` + `HERMES_TELEMETRY_HOME` pinned to
  `C:\Users\Sovav\AppData\Local\Temp\htc-c1-home`; plugin loaded via
  `plugins.discover_plugins()`; rows read back read-only from the disposable
  `telemetry/telemetry.db`. Real-home leak check after every run. (These two
  runs predate the three-pin ruling; their gate-2 board tick was invalidated
  by C1-B1 and replaced by the re-run below.)
- HTC-C1-FIX re-run (`htc-c1-fix-driver.py`): the gate-2 dispatch tick and the
  HARNESS-A pricing comparison re-executed under full three-pin isolation
  (all `HERMES_KANBAN_*` surfaces redirected into `htc-c1-fix-home`); leak
  check covers BOTH the live board DB (zero row delta) and `telemetry.db`
  (`c1_fix/c1_gate rows=0`).
- Static: every runtime citation above grepped/verified against the installed
  v0.21.3 source at `C:/Users/Sovav/AppData/Local/hermes/hermes-agent`
  (READ-ONLY) and the harness repo at `41bba4d` (READ-ONLY, dirty working
  tree as noted in Ground truth).
