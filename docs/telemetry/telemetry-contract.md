# SHARED TELEMETRY CONTRACT — sovav-hermes + Turnstone observability plane

- **Version:** 1.0.0
- **Status:** contract-freeze candidate (pending independent G2 review; not yet load-bearing)
- **Canonical owner:** sovav-hermes (turnstone companion plan D8: "canonical envelope schema lives on sovav-hermes … neither repo edits the contract unilaterally")
- **Consumers:** sovav-hermes harness instrumentation (R-OBS-1x) and Turnstone application instrumentation (T-OBS-1x). This is the R-OBS-00 fan-in gate: neither repo proceeds past contract-freeze without it (opus-review B4).
- **Machine-readable schema:** `docs/telemetry/correlation-envelope.schema.json` (this repo), JSON Schema draft 2020-12, `$id` `urn:sovav-hermes:telemetry:correlation-envelope:1.0.0`, `"version": "1.0.0"`.

## 0. Inputs (authoritative, read in full before this contract was written)

1. `t_c35a9b70` attachments `revised-plan.md` — section 5 (observability workstream, T3) and section 1 corrections T1–T5.
2. `t_c35a9b70` attachment `revised-queue.json` — R-OBS-01..04 acceptance tests this contract must support.
3. `t_1a55f3a6` `opus-review.md` — section B4 (this tranche's charter), M5 (verified instrumentation surfaces), M6 (credential-availability failure route).
4. Turnstone board `t_09c3a2d3` `turnstone-companion-plan.md` — PM decisions D1–D8 (BINDING) and section 8 (paired-deployment contract; verified point-by-point in section 10 of this document).

Where any conflict exists, the shared sovav-hermes plan wins on shared components and the companion plan wins on Turnstone repo internals (companion plan preamble). Schema changes flow through the paired plan; neither repo edits this contract unilaterally (D8). Schema-contract changes are a Trevor escalation class (companion plan section 7).

## 1. Correlation envelope

One envelope, propagated everywhere, emitted by both repos. LangSmith trace metadata and Sentry tags carry the same envelope so a kanban event, a LangSmith trace and a Sentry issue join on it (revised-plan §5 CORRELATION).

### 1.1 Canonical base fields (owned by sovav-hermes, D8)

The base field set is EXACTLY these twelve fields — no additions, no omissions:

| # | Field | Type | Required | Nullable | Meaning / rules |
|---|-------|------|----------|----------|-----------------|
| 1 | `board` | string | yes | no | Board slug of the originating card (`sovav-hermes` or `turnstone`). |
| 2 | `task_id` | string | yes | no | Board task id of the card being worked (e.g. `t_2731ee50`). |
| 3 | `run_id` | integer or string | yes | no | Dispatch/run identifier of the emitting run. Hermes run ids are integers; emitters MAY serialize them as strings. Consumers MUST compare run ids as strings. |
| 4 | `parent_task_ids` | array of string | yes | no (may be empty) | Parent card ids of `task_id`; empty array when the card has no parents. |
| 5 | `profile/role` | string | yes | no | Emitting Hermes profile, which is also the role (`operator`, `pm`, `tm`, `worker`, `reviewer`). The `default`/`Hermes` key/profile is interactive human use only — autonomous spend attributed to it is an attribution red flag (revised-plan §3) and MUST be surfaced under the attribution-anomaly alert class (section 5). |
| 6 | `model` | string | yes | no | Provider-qualified model id as invoked (e.g. `z-ai/glm-5.3-flash`). |
| 7 | `provider` | string | yes | no | Model provider id (e.g. `openrouter`, `anthropic`). |
| 8 | `key_id` | string | yes | no | NON-SECRET credential label — key name or hash prefix only. NEVER a key value. This field exists so attribution works without any secret material ever leaving the secret channel. |
| 9 | `call_class` | enum | yes | no | Exactly one of `primary`, `fallback`, `aux`, `delegate`, `judge`, `cron`. |
| 10 | `handoff_id` | string | yes | yes | Handoff/transition identifier when the event is part of a worker→reviewer or card→card handoff; `null` otherwise. |
| 11 | `worktree/branch` | string | yes | no | Git branch the run executed on, which identifies the isolated worktree (e.g. `r-obs-00`). |
| 12 | `trace_id` | string | yes | no | Cross-repo trace identifier. One half of the cross-repo join key (1.4). |

Note on key names: the slash characters in `profile/role` and `worktree/branch` are part of the canonical field name, verbatim from revised-plan §5 and companion D2. JSON keys may legally contain `/`. In transports that forbid slashes in key names (Sentry tag keys allow only `[A-Za-z0-9_.:-]`), the transport mapping in 1.6 applies.

### 1.2 Turnstone extension fields (companion D2, verbatim)

The Turnstone extension set is EXACTLY these twelve fields — no additions, no omissions. All are metadata-only. Off Turnstone (harness-originated events) each extension field is `null` or absent; on Turnstone-originated events (`repo == "turnstone"`) all twelve are required and NON-NULL — the machine-readable schema enforces this by narrowing each extension to its non-null type (identifiers non-empty; `runtime_environment` its non-null enum) inside the `repo == "turnstone"` branch (1.3):

| # | Field | Type (on Turnstone events) | Meaning / rules |
|---|-------|---------------------------|-----------------|
| 1 | `repo` | string, `"turnstone"` | Marks the event as Turnstone-originated. On harness-originated events this field is `null` or absent (1.3). |
| 2 | `agent_run_id` | string | Turnstone AgentRun identifier. |
| 3 | `correlation_id` | string | Turnstone correlation identifier. Other half of the cross-repo join key (1.4). |
| 4 | `idempotency_key` | string | Duplicate-export idempotency key; duplicate export of the same `agent_run_id` + `correlation_id` is idempotent (companion §5). |
| 5 | `deal_id` | string | Opaque internal id (`newId("deal")`) — safe metadata per D2. |
| 6 | `worker_id` | string | Turnstone worker identifier. |
| 7 | `model_ref` | string | The worker definition's model reference. |
| 8 | `trigger_type` | string | Run trigger type per the Turnstone trigger policy (automatic triggers stay disabled; production posture 1% manual-only, eval-gated — companion §3). |
| 9 | `runtime_environment` | enum `dev`, `staging`, `production` | From `WorkerRunContract.runtime_environment` (D4); drives the Sentry environment tag (section 2). |
| 10 | `basis_fingerprint` | string | Basis fingerprint; explicitly export-approved metadata (D3). |
| 11 | `worker_definition_version` | string | Version of the worker definition that ran. |
| 12 | `output_schema_version` | string | Version of the output schema the worker was contract-bound to. |

### 1.3 Required-set convention

- **Base fields:** required on EVERY envelope, from both repos.
- **Turnstone extension fields (all twelve of 1.2) plus `harness_task_id` / `harness_run_id` (1.5):** required on Turnstone-originated events (i.e. when `repo == "turnstone"`), and must be `null` or absent otherwise.
- This convention is expressed machine-readably in `correlation-envelope.schema.json` with draft-2020-12 `if/then` blocks: a `repo` const-`"turnstone"` condition triggers the extension `required` set AND narrows every Turnstone extension field to its non-null type (identifiers `string` with `minLength: 1`; `runtime_environment` its non-null `dev`/`staging`/`production` enum); its negation forces every extension field to JSON `null` when present. Only `harness_task_id` / `harness_run_id` stay nullable on Turnstone-originated runs (for non-harness-originated Turnstone runs), governed by the pair-consistency rule. One additional consistency rule is expressed the same way: `harness_task_id` and `harness_run_id` are carried together — a non-null value for either forces a non-null value for the other.

### 1.4 Cross-repo join key

**`trace_id` + `correlation_id`.** A kanban event, a LangSmith trace and a Sentry issue produced by the same logical run join on this pair when the run is Turnstone-originated (the harness side contributes `trace_id`; Turnstone contributes `correlation_id`). Within the harness repo alone, kanban events, traces and issues join on `task_id` + `run_id`. The Turnstone replayed-scenario acceptance (companion §5: kanban event ↔ LangSmith trace ↔ Sentry issue on `correlation_id`/`trace_id`, harness ids present when propagated, `null` otherwise) is defined by exactly these two join rules.

### 1.5 Harness-origin propagation

When a Turnstone run originates from a harness card, the harness dispatcher passes the environment variables `TURNSTONE_HARNESS_TASK_ID` and `TURNSTONE_HARNESS_RUN_ID` into the Turnstone run. The envelope then carries:

- `harness_task_id` — string, populated from `TURNSTONE_HARNESS_TASK_ID`;
- `harness_run_id` — string, populated from `TURNSTONE_HARNESS_RUN_ID`;
- otherwise both are `null`.

Both are carried together (1.3). This is the mechanism T-OBS-04 and the Turnstone replay test rely on for "harness task/run ids present when propagated, null otherwise".

### 1.6 Transport mapping

- **LangSmith:** the envelope is attached as trace/metadata keys verbatim (JSON keys with `/` are legal in LangSmith metadata).
- **Sentry:** the envelope is attached as tags; tag keys containing `/` are serialized with the slash replaced by an underscore (`profile/role` → `profile_role`, `worktree/branch` → `worktree_branch`). This normalization is deterministic and part of this contract; `run_id` is serialized as a string in tag values.
- Both transports carry the same logical values; a join on the envelope is therefore transport-independent.

### 1.7 Schema artifact, ownership and pinning

- Canonical path: `docs/telemetry/correlation-envelope.schema.json` in the sovav-hermes repo (this worktree, branch `r-obs-00`). Version: `1.0.0`.
- Turnstone pins a copy at `fixtures/telemetry/correlation-envelope.schema.json` with a compatibility test that goes red on drift (D8). The pin is of version `1.0.0` exactly.
- **R-OBS-00 is the authoritative publisher** of the schema path and version: the R-OBS-00 completion metadata publishes both so T-OBS-04 can pin them (companion §8 item 2). R-OBS-01 is not the publisher; if R-OBS-01 needs the values, it carries them forward from R-OBS-00's published metadata.
- Additive evolution only within a major version; breaking changes bump the major version and flow through the paired plan (D8; Trevor escalation class).

## 2. Environment / project / release separation

### 2.1 LangSmith — per-environment projects (companion D4)

Six projects total; the repo prefix is the hard separator between repos — no project is ever shared across repos:

| Project | Repo | Purpose |
|---|---|---|
| `harness-dev` | sovav-hermes | Harness instrumentation, dev environment |
| `harness-staging` | sovav-hermes | Harness instrumentation, staging environment |
| `harness-production` | sovav-hermes | Harness instrumentation, production environment |
| `turnstone-dev` | Turnstone | Turnstone instrumentation, dev environment |
| `turnstone-staging` | Turnstone | Turnstone instrumentation, staging environment |
| `turnstone-production` | Turnstone | Turnstone instrumentation, production environment |

LangSmith separates by project (D4); there is no cross-repo project and no environment tag multiplexing inside a single LangSmith project.

### 2.2 Sentry — single project per repo, environment tag (companion D4)

Exactly two Sentry projects:

| Project | Repo | Discrimination |
|---|---|---|
| `harness` | sovav-hermes | `environment` tag: `dev` \| `staging` \| `production` |
| `turnstone` | Turnstone | `environment` tag: `dev` \| `staging` \| `production` (from `WorkerRunContract.runtime_environment`, D4) |

### 2.3 Release formats (companion D4)

- Format: `harness@<version>+<sha>` and `turnstone@<version>+<sha>`.
- `<sha>` = `TURNSTONE_BUILD_SHA` if that env var is set, else `git rev-parse HEAD` of the repo being released.
- `<version>` = the version identifier the releasing repo records for the release (Turnstone: its package version, per D4; harness: the version recorded by the releasing tranche, e.g. the R-GATE-01 release memo).
- **Harness release gate:** the gate requires a Sentry release existing on the `harness` project (revised-queue R-OBS-03 control point; R-GATE-01 ties to it).

## 3. Shared redaction rules and secret handling

### 3.1 Metadata-only defaults (both repos)

- The export default is METADATA ONLY: envelope fields, counts, ids, error codes, timing/latency, cost/token aggregates, fingerprints and labels.
- Prompts and tool outputs are NEVER exported by default (revised-plan §5 PRIVACY).
- Redaction applies BEFORE export — an export path may never see un-redacted material and then rely on the sink's scrubbing.

### 3.2 Never-export lists

**Turnstone never-export list (companion D3, verbatim):**
`runtime_context`, `raw_capture`, party/property/contact fields, prompts, `structured_output` payloads, secrets, `.env` values.
Export-approved Turnstone metadata (D3): AgentRun telemetry fields, counts, `payload_hash`, `basis_fingerprint`, error codes, `redacted_debug_summary` (already redacted at source).

**Harness never-export list (written explicitly here, as required):**
1. prompts (any prompt text or prompt template body);
2. tool-result bodies (payload content returned by tools into the model context);
3. transcript/approval material (conversation transcripts, approval requests/approval content, protected-file staging content);
4. secrets (API keys, tokens, DSNs, credentials of any kind).
Everything outside this list is still constrained by 3.1: the default export is metadata only.

### 3.3 Redaction-leakage scan — required tested failure mode (both repos)

Every export path is covered by a redaction-leakage test that scans the ACTUAL exporter output (exported spans/events/issues, not source fixtures) against fixture sentinel marker values; any hit fails. This is a required tested failure mode in both repos (revised-plan §8; companion §5), and a scan hit also raises the `redaction leakage` alert class (section 5).

### 3.4 Credential handling

- Credentials (LANGSMITH_API_KEY, SENTRY_DSN, OpenRouter keys) enter ONLY via the approved secret channels as env/config; Turnstone documents credential env-var names in `.env.example` names-only (companion §6).
- No secrets ever in repo, tasks, logs, traces, prompts, artifacts or chat (T3, revised-plan §1, companion §6).
- Missing or unauthorized observability credentials ⇒ `needs_input` to Trevor (billing/secret authority) — never autonomous provisioning, never a secret in a card body (opus-review M6). This is the credential-availability failure route for R-OBS-02/03 and T-OBS-02/03.

## 4. Direct-ingestion strategy

- **Harness side:** direct SDK ingestion from the Hermes plugin layer to LangSmith and Sentry (revised-plan §5 INGESTION; R-OBS-01 write scope). The instrumented surfaces are the verified ones (opus-review M5): tool middleware (`agent/agent_runtime_helpers.py:2296-2298, :2341`) and verified hook-capable plugin siting only.
- **Turnstone side:** in-repo fetch-based exporters behind a `TelemetryExporter` interface, Noop default, ZERO new runtime dependencies (D1). langsmith/@sentry SDKs are rejected by default; an SDK may replace the fetch exporter ONLY via a recorded verdict naming the capability the fetch path cannot meet (D1).
- **OpenRouter → LangSmith Broadcast path:** stays EXCLUDED unless independently fixed and proven by a bounded spike with a recorded receipt (T3, revised-plan §5, D1). No tranche may depend on it.
- Exporters are read-only observers at the instrumentation boundary; they cannot approve, execute protected actions, mutate lifecycle, or route around TransitionEngine/ApprovalController/ExecutionController (companion §3; observability is evidence, never enforcement — T3).

## 5. Alert-ownership matrix

Routing is deterministic and non-LLM: every alert class routes to exactly one owning role's durable channel (R-OBS-04; companion D5). Zero routine Operator inference; Operator chat closed; delivery reuses the notifier's existing durable delivery checkpoint and its idempotency — duplicate-alert idempotency reuses the notifier durable checkpoint (M-c; revised-queue R-OBS-04). No new idempotency code.

The owning-role assignments below are the contract's documented defaults; the implementing tranches (R-OBS-04 / T-OBS-05) record the live mapping, and changes route through the paired plan.

### 5.1 Harness alert classes (extending revised-plan §5 / §8)

| Alert class | Trigger evidence | Owning role (default) |
|---|---|---|
| blocker-delivery failure | a `blocked(kind=needs_input/capability)` event is not durably delivered/acknowledged via the non-Operator path (notifier durable checkpoint, M-c) | operator |
| dispatch stall | eligible ready task not auto-started within the stall window, or claimed-then-crashed before initialization (defect class of t_ba807768 run 35: claim+spawn then init crash) | tm |
| telemetry export failure | exporter bounded queue overflow or drop-with-event observed on a sink | pm |
| redaction leakage | redaction-leakage scan or runtime sentinel detects never-export material in an exported payload (3.3) | operator (escalate to Trevor on confirmed secret material) |
| attribution anomaly | unattributed cron/delegate path; autonomous spend on the default/`Hermes` key; unknown-never-zero billing entry; cancelled-but-billed or restart-mid-reservation ledger anomaly | pm |
| routing mismatch | key/model selection outside the declared role routing row (revised-plan §3 routing isolation test) | tm |
| provider outage | persistent provider failure rate on a role's primary call path with declared fallback not engaged (revised-plan §8 provider-outage test) | operator |

### 5.2 Turnstone alert RULES (companion D5 — Turnstone defines RULES only; delivery channel stays shared)

| Rule | Owning role tag (default) |
|---|---|
| worker provider_error rate | operator |
| eval-gate failure | operator |
| rollout block-count drift | pm |
| failed AgentRun count | pm |
| telemetry export failure | pm (mirrors the harness export-failure class) |

Rule thresholds are documented configurable DEFAULTS, set at T-OBS-05 implementation time from measured baselines — never invented (B5 discipline: thresholds measured first).

### 5.3 Reserved Turnstone tag namespace (paired-contract para 5)

- `owning_role=operator|pm`
- `environment=dev|staging|production`
These tag values are reserved for Turnstone alert rules delivered through the shared R-OBS-04 channel; the harness does not emit them.

## 6. Seer workflow and usefulness-evaluation protocol

- Seer is enabled on BOTH Sentry projects (`harness`, `turnstone`).
- Protocol (before any Seer output becomes load-bearing):
  1. Pick a NAMED defect class (named in advance, not discovered ad hoc).
  2. Have Seer analyze real events of that class.
  3. Record a verdict — `useful` or `not useful` — with evidence, on the owning card.
  4. Only a recorded verdict may make Seer analysis load-bearing.
- Seer never auto-applies fixes. Observability is EVIDENCE, never enforcement and never permission (T3).
- Named defect classes:
  - **Turnstone:** `provider_error OPENAI_TIMEOUT` and `WORKER_OUTPUT_SCHEMA_INVALID` (D6).
  - **Harness:** the crashed-run-on-dispatch defect class (claim+spawn then initialization crash, evidenced by t_ba807768 run 35 — the same class the dispatch-stall alert watches).

## 7. Kill switch and export-failure behavior

- **A single flag disables ALL export (tracing/performance/alerts) without touching any enforcement gate.**
- **Harness flag:** `observability.export_enabled` — toggled via `hermes config set` / `config get` with read-back (config changes never by hand-editing config files).
- **Turnstone flags (D7):** `TURNSTONE_TELEMETRY` unset or `"off"` ⇒ Noop exporter (this is the default). Per-sink overrides: `TURNSTONE_TELEMETRY_LANGSMITH`, `TURNSTONE_TELEMETRY_SENTRY`.
- **Export failure:** bounded local queue, then drop-with-event. The gated call path NEVER blocks on export (R-OBS-01 failure handling; D7).
- **Rollback:** flag off + read-back (harness: `config get observability.export_enabled` shows the flag off; Turnstone: env off + read-back; no registry or source-authority edits, weaker than the wave1 registry-only rollback path — D7).
- **Disabled-telemetry invariant (Turnstone, D7):** a test proves authority records (AgentRun/ProposedWrite/AcceptedWrite hashes) are identical with telemetry on vs off, and the validation suite passes in both modes. Telemetry being off must never change behavior other than visibility.

## 8. Retention and sampling policy defaults per project

Every number in this section is a **documented configurable DEFAULT** — not a measured result. No invented-savings or measured-threshold claims (B5 discipline); tuning happens only from measured baselines at implementation time.

### 8.1 LangSmith (six projects)

| Project | Trace sampling DEFAULT | Retention DEFAULT |
|---|---|---|
| `harness-dev` | 100% | shortest retention tier available on the paid plan; exact tier recorded at R-OBS-02 provisioning |
| `harness-staging` | 100% | same rule as `harness-dev` |
| `harness-production` | 50% uniform baseline; `call_class` in {primary, judge, delegate} forced 100% | same rule as `harness-dev` |
| `turnstone-dev` | 100% | shortest retention tier available on the paid plan; exact tier recorded at T-OBS-02 provisioning |
| `turnstone-staging` | 100% | same rule as `turnstone-dev` |
| `turnstone-production` | 50% uniform baseline; `call_class` in {primary, judge, delegate} forced 100% | same rule as `turnstone-dev` |

### 8.2 Sentry (two projects)

| Project | Error capture | Performance trace sampling DEFAULT (per environment tag) | Retention |
|---|---|---|---|
| `harness` | 100% of error events (never sampled down) | dev 1.0 · staging 1.0 · production 0.25 | Sentry plan default retention; recorded at provisioning; not set by this contract |
| `turnstone` | 100% of error events (never sampled down) | dev 1.0 · staging 1.0 · production 0.25 | same rule as `harness` |

## 9. Consumer support map (how the contract supports downstream acceptance tests)

- **R-OBS-01** (envelope emitted on a replayed offline scenario, joins across kanban event + LangSmith trace + Sentry tag; metadata-only default proven; kill switch with read-back): envelope §1, metadata-only defaults §3.1, kill switch §7.
- **R-OBS-02** (traces carry envelope; redaction-leakage test; sampling/retention configured before live traces): §1.6, §3.3, §8.1.
- **R-OBS-03** (release + environment mapping; scrubbing before first event; Seer workflow documented): §2, §3.1, §6.
- **R-OBS-04** (deterministic alert routing with Operator chat closed; Seer usefulness verdict; duplicate-alert idempotency): §5, §6, §5 idempotency via notifier durable checkpoint.
- **T-OBS-01** (contract + exporter scaffold, Noop, env gating, SDK-vs-fetch spike verdict): §4, §7, §1.7.
- **T-OBS-02 / T-OBS-03** (LangSmith / Sentry exporters, zero-dependency, network-fenced): §2, §3, §4.
- **T-OBS-04** (cross-repo correlation join proof + schema drift test): §1.4 join key; §1.7 pinned copy at `fixtures/telemetry/correlation-envelope.schema.json` matching version `1.0.0`.
- **T-OBS-05** (alert rules, Seer usefulness, disabled-telemetry/outage/loss/retry/duplicate matrix): §5.2/§5.3, §6, §7.
- **T-OBS-06** (evidence memo feeding R-GATE-01 / R-ENTRY-01): the evidence classes it must assemble are named in §3.3 (redaction scan), §7 (disabled-telemetry hash-equality) and §1.4 (correlation join transcript).

**Cross-repo dependency gates (companion §8 item 3).** Exactly three hard gates exist between the repos; everything else is side-by-side with no additional serialization:

1. **X0** — the Opus verdict on `t_1a55f3a6` blocks BOTH repos (neither side proceeds past contract-freeze without it).
2. **T-OBS-04 ← R-OBS-01** — the Turnstone cross-repo correlation join proof requires R-OBS-01's envelope emitted on a replayed offline scenario.
3. **T-OBS-05 ← R-OBS-04** — the Turnstone alert rules require R-OBS-04's shared alert delivery channel.

All other Turnstone work (T-OBS-01..03, T-OBS-06) and all other harness work runs side-by-side.

## 10. Paired-deployment contract verification table (companion plan section 8, point by point)

| # | Companion §8 item | Where this contract addresses it |
|---|---|---|
| 1 | Turnstone consumes the shared correlation envelope, redaction/retention/sampling policy, secret-channel convention and alert delivery channel from R-OBS-01..04; do not rebuild or fork them per-repo | §1 (envelope written once, canonical here), §3 (redaction/secret rules shared), §8 (retention/sampling shared), §5 (shared alert delivery). This document IS the single shared source; Turnstone consumes, never duplicates. |
| 2 | Canonical envelope schema owned by sovav-hermes; Turnstone pins a copy at `fixtures/telemetry/correlation-envelope.schema.json` with a drift test (D8); publish schema path + version in R-OBS-01 completion metadata so T-OBS-04 can pin it | §1.7: canonical path + version `1.0.0` — **R-OBS-00 is the authoritative publisher** (this document + the R-OBS-00 worker card's completion metadata); R-OBS-01 carries the values forward only, it does not publish them; drift-test pinning defined. |
| 3 | Cross-repo hard gates: X0 = Opus verdict t_1a55f3a6 (blocks both sides); T-OBS-04 needs R-OBS-01's envelope on a replayed scenario; T-OBS-05 needs R-OBS-04's alert channel; everything else side-by-side | §9 states all three gates explicitly (X0; T-OBS-04 ← R-OBS-01; T-OBS-05 ← R-OBS-04) plus the side-by-side rule; this contract is the R-OBS-00 fan-in gate (opus-review B4) and imposes no serialization of the two repos beyond them. |
| 4 | Turnstone exporters in-repo fetch-based, zero-dependency, env-gated Noop-by-default (D1/D7); harness instrumentation stays in the Hermes plugin layer; the two instrumentations share ONLY the contract | §4 (both sides' ingestion strategies; SDK-verdict rule; Broadcast exclusion), §7 (env gating/Noop default). |
| 5 | Turnstone alert RULES (owning_role tags operator/pm) delivered through the shared R-OBS-04 channel; reserve the tag namespace `owning_role=operator|pm` and `environment=dev|staging|production` for Turnstone | §5.2 (the five D5 rules), §5.3 (reserved namespace). |
| 6 | Turnstone evidence (T-OBS-06 memo: baseline vs post-change, correlation join transcript, redaction scan, disabled-telemetry hash-equality proof) feeds the shared R-GATE-01 release gate and R-ENTRY-01 alpha brief | §9 names the contract sections each evidence class comes from; §3.3, §7, §1.4 define the evidence requirements the memo must reference. |
| 7 | Turnstone queue T-OBS-01 → parallel T-OBS-02 + T-OBS-03 → T-OBS-04 → T-OBS-05 → T-OBS-06, full detail on the turnstone board | §9 support map covers T-OBS-01..06; the queue itself remains owned by the turnstone board (t_09c3a2d3 attachments) — this contract does not re-state or fork it. |
| 8 | Turnstone authority invariants non-negotiable: telemetry is evidence, never permission; no protected surface opens; validation suite passes on every Turnstone tranche PR | §4 (exporters are read-only observers, never authority), §6 (T3 protocol), §7 (kill switch cannot touch enforcement gates). The validation suite is Turnstone-repo discipline (companion §3) and is referenced, not duplicated, here. |

## 11. What this contract does NOT do

No implementation, no code, no config changes, no dispatch, no paid probes, no live telemetry calls, no credentials. It sets no measured thresholds and makes no savings/ETA claims. It creates no cards and changes no enforcement surface; observability stays evidence, never enforcement (T3).
