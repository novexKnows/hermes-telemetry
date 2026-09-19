# Install / Rollback Runbook — hermes-telemetry on disposable HERMES_HOME (executed)

- **Status:** executed end-to-end — every command below was actually run, and the
  "Expected output" blocks are the real captured output of that run (no
  reconstruction, no placeholders). This is the **corrected re-execution** after
  review round 1 (task `t_ac41c94b`): archive pinned to the immutable integration
  SHA, home overrides moved before every runtime invocation (including
  `hermes --version`), the version call's artifacts added to the rollback,
  and an explicit environment-rollback step added.
- **Executed:** 2026-09-19 (Eastern Daylight Time)
- **Runtime:** Hermes Agent v0.21.3 (2026.9.14), upstream 0ddba07a, editable
  install at `C:\Users\Sovav\AppData\Local\hermes\hermes-agent` (Python 3.11.16,
  venv `...\hermes-agent\venv`) — used **read-only**; the CLI is invoked, never
  modified.
- **Plugin source:** hermes-telemetry integration tip, archived from the
  **immutable SHA `9a66874`** (`git archive 9a66874` of branch
  `wt/htc-integration`). Do NOT archive `HEAD`: `HEAD` moves as this runbook's
  own commits land on the worktree branch, and `git archive HEAD` on the
  corrected branch exports **124** files (the runbook commits included) instead
  of the expected **123** — this exact drift was caught in review round 1.
- **Platform:** Windows 11, git-bash (MSYS) shell.
- **Proof method:** full file inventory + SHA-256 of every file in the disposable
  home, before install and after rollback, diffed; plus a live-board row-count
  leak check around the kanban-correlation probe (§5).
- **Credentials needed:** none. This runbook performs no network/LLM calls and
  requires no sandbox or provider credentials.

---

## 0. Conventions and isolation rules (read before running)

1. **Fresh shell, absolute paths.** Run every step from a fresh git-bash shell.
   Nothing below depends on prior state.
2. **Two overrides, not one.** EVERY runtime invocation must set BOTH:

   ```
   export HERMES_HOME=<disposable home root>
   export HERMES_TELEMETRY_HOME=<disposable home root>
   ```

   `paths.py:32` resolves the telemetry directory as
   `HERMES_TELEMETRY_HOME || HERMES_HOME`, **then appends `/telemetry` itself**:

   ```python
   override = os.environ.get("HERMES_TELEMETRY_HOME") or os.environ.get("HERMES_HOME")
   root = Path(override) if override else Path.home() / ".hermes"
   return root / "telemetry"
   ```

   Setting `HERMES_TELEMETRY_HOME` to a `.../telemetry` path nests
   `telemetry/telemetry/` and silently splits plugin state from runtime state.
   (This exact trap was hit while executing this runbook — the driver's assert
   now guards it.)
3. **`HERMES_TELEMETRY_NO_SETUP=1` must be set for every invocation.** Without
   it, the plugin's first-load auto-setup (`setup.py:278 run()`,
   non-interactive branch) **writes a default `budget.yaml`**
   (`$5/day, $100/month`) into the telemetry dir — that silently turns budget
   enforcement ON. Verified in the integrated source (`setup.py`, "Non-
   interactive: write recommended defaults" branch).
4. **Never point any of this at a real home.** No step below reads or writes
   `~/.hermes`. The driver asserts `HERMES_HOME` is the disposable path and
   aborts otherwise.
5. `C:/tmp/htc-c2-runbook` is scratch space (capture dir + disposable home);
   recreate it freely. The worktree `C:/tmp/hermes-telemetry/c2-runbook` is only
   read from (`git archive`) — one pip-probe exception in §2, which writes
   nothing.
6. **Three-pin isolation for kanban-correlation exports.** §5 stamps the
   plugin's envelopes with `HERMES_KANBAN_*` values. If you export ANY kanban
   correlation var, you MUST also override BOTH `HERMES_KANBAN_DB` and
   `HERMES_KANBAN_BOARD` to disposable values — a worker/CLI context can mutate
   the LIVE board through them (the runtime fence, `hermes_cli/kanban_db.py:147`,
   only protects `delegate_task` child contexts, not your own shell). §5 uses a
   non-live board name and a nonexistent disposable DB path, and brackets the
   probe with a **read-only row-count leak check** on the live board DB
   (`capture/live_board_leak_check.py`): the counts must be identical before and
   after.

---

## 1. Pre-install state: disposable home + inventory BEFORE

```bash
rm -rf C:/tmp/htc-c2-runbook && mkdir -p C:/tmp/htc-c2-runbook/capture C:/tmp/htc-c2-runbook/disposable-home
cd C:/tmp/htc-c2-runbook
find disposable-home -type f | wc -l
find disposable-home -type f -exec sha256sum {} + | sort > capture/inventory-before.txt
```

Expected output (real):

```
0
```

`inventory-before.txt` is empty: the disposable home starts with **0 files**.
Every file that exists later was created by the install/verify run, and the
rollback in §6 must remove all of them.

The home overrides are exported **BEFORE** the first runtime invocation —
including the `hermes --version` identity check below. (`--version` is a real
runtime invocation: it performs an update check and **creates
`<HERMES_HOME>/.update_check` and `<HERMES_HOME>/SOUL.md`**. With the overrides
in place those land in the disposable home, where §6.3 removes them; without
them they would pollute the real home — this ordering was a review-round-1
blocker.)

```bash
export HERMES_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_NO_SETUP=1
hermes --version
```

Expected output (real, exit 0):

```
Hermes Agent v0.21.3 (2026.9.14) · upstream 0ddba07a
Install directory: C:\Users\Sovav\AppData\Local\hermes\hermes-agent
Install method: git
Python: 3.11.16
OpenAI SDK: 2.24.0
Update available: 780 commits behind — run 'hermes update'
```

(The `Update available` line is part of the real output; the behind-count grows
as upstream moves — 780 is what this execution captured. The two files this call
created are real run artifacts, enumerated and removed in §6.2–§6.3.)

---

## 2. Install — plugin-directory mechanism (NOT pip)

**Known caveat: pip cannot build this repo.** Both `pip install -e .` and
`pip install .` fail: `pyproject.toml` pins
`[tool.setuptools.packages.find] include = ["hermes_telemetry*"]`, but the repo
directory is hyphenated (`hermes-telemetry`) and the modules are top-level flat
files — package discovery matches nothing, so setuptools produces 0 egg-info
dirs and pip aborts at metadata generation. Re-executed for this runbook:

```bash
cd C:/tmp/hermes-telemetry/c2-runbook
pip install --no-deps -e . --disable-pip-version-check
```

Expected output (real, exit code 1; middle elided where repetitive):

```
Obtaining file:///C:/tmp/hermes-telemetry/c2-runbook
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Checking if build backend supports build_editable: started
  Checking if build backend supports build_editable: finished with status 'done'
  Getting requirements to build editable: started
  Getting requirements to build editable: finished with status 'done'
  Preparing editable metadata (pyproject.toml): started
  Preparing editable metadata (pyproject.toml): finished with status 'error'
  error: subprocess-exited-with-error

  × Preparing editable metadata (pyproject.toml) did not run successfully.
  ...
        File "...setuptools\build_meta.py", line 360, in _find_info_directory
          assert len(candidates) == 1, (
      AssertionError: Exactly one .egg-info should have been produced, but found 0: []
      [end of output]

  note: This error originates from a subprocess, and is likely not a problem with pip.
error: metadata-generation-failed

× Encountered error while generating package metadata.
╰─> from file:///C:/tmp/hermes-telemetry/c2-runbook
```

**The supported install path for this runtime is the user plugin directory:**
runtime v0.21.3 discovers directory plugins from `<HERMES_HOME>/plugins/<name>/`
(verified in `hermes_cli/plugins_discovery.py`:
`user_dir = get_hermes_home() / "plugins"`, honoring `HERMES_HOME`), and a
directory plugin needs a `plugin.yaml` manifest — which this repo ships at its
root. Install = copy the approved tree into that directory, archived from the
**immutable integration SHA** (see the header note on why not `HEAD`):

```bash
cd C:/tmp/hermes-telemetry/c2-runbook
mkdir -p C:/tmp/htc-c2-runbook/disposable-home/plugins/hermes-telemetry
git archive 9a66874 --format=tar | tar -x -C C:/tmp/htc-c2-runbook/disposable-home/plugins/hermes-telemetry
find C:/tmp/htc-c2-runbook/disposable-home/plugins/hermes-telemetry -type f | wc -l
```

Expected output (real):

```
123
```

(`git archive 9a66874` exports the approved integration tip without `.git`,
build artifacts, or uncommitted files — exactly 123 files. The same command
against `HEAD` on this branch exports 124.)

---

## 3. Enable — `hermes plugins enable` from the INSTALLED runtime

All remaining steps run under the disposable environment (exported in §1; the
exports are repeated here so each block is copy-paste-runnable on its own):

```bash
export HERMES_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_NO_SETUP=1
```

Plugins are opt-in; before enabling, the CLI sees it as a disabled user plugin:

```bash
hermes plugins list --plain --no-bundled
```

Expected output (real):

```
not enabled  user     0.8.0    hermes-telemetry
```

Enable:

```bash
hermes plugins enable hermes-telemetry
```

Expected output (real, exit 0):

```
✓ Plugin hermes-telemetry enabled. Takes effect on next session.
✓ Installed Python dependencies: watchdog>=3.0, jsonschema>=4.18
Allow this plugin to replace built-in tools (e.g. shell_exec, write_file)?
  This is a privileged capability: an override can intercept everything the
agent routes through that tool.
  Grant it?  hermes-telemetry may not override built-in tools. Re-run `hermes plugins enable
hermes-telemetry --allow-tool-override` to grant this later.
```

(The dependency line is a no-op on this machine — watchdog 6.0.0 and jsonschema
4.26.0 were already in the runtime venv. The tool-override prompt is denied by
default: this plugin declares no tool overrides; `config.yaml` records
`allow_tool_override: false`.)

Confirm enabled:

```bash
hermes plugins list --plain --no-bundled
```

Expected output (real):

```
enabled      user     0.8.0    hermes-telemetry
```

The enable step wrote `config.yaml` into the disposable home (the runtime also
appends generated comment sections — Security, Fallback Model — elided here;
the plugins block is what the enable step wrote):

```yaml
plugins:
  enabled:
    - hermes-telemetry
  disabled: []
  entries:
    hermes-telemetry:
      allow_tool_override: false
_config_version: 45
```

---

## 4. Configure the integration profile with budget features OFF

Budget knobs, verified from the integrated code (not assumed):

| Knob | Where | OFF value used here |
|------|-------|---------------------|
| `budget.yaml` file | `paths.py:44 get_budget_path()` → `<telemetry home>/budget.yaml` | **absent** — "absence of budget.yaml is the 'budgets disabled' signal" (`paths.py` module docstring) |
| first-load auto-setup | `__init__.py` register() tail; `setup.py:278 run()` | `HERMES_TELEMETRY_NO_SETUP=1` — without it, non-interactive auto-setup **writes** a default `budget.yaml` (§0.3) |
| envelope emission | `envelope.py:130 kill_switch_enabled()` (`HERMES_TELEMETRY_ENVELOPE_ENABLED`) | left unset = **ON** (default), so event flow is observable |

Proof that budgets are OFF under this configuration:

```bash
test -f C:/tmp/htc-c2-runbook/disposable-home/telemetry/budget.yaml \
  && echo "budget.yaml EXISTS" || echo "budget.yaml ABSENT (budgets disabled)"
```

Expected output (real):

```
budget.yaml ABSENT (budgets disabled)
```

(There is no `plugins.entries.hermes-telemetry.settings.*` budget switch — the
only budget control surface is the `budget.yaml` file itself. Setting any
`daily_usd`/`monthly_usd` there turns enforcement ON.)

---

## 5. Verify event flow — through the runtime's own plugin machinery

No LLM call is made. The driver below loads plugins exactly like the runtime
does (`hermes_cli.plugins.discover_plugins()`), fires lifecycle hooks through
the runtime's dispatch (`hermes_cli.plugins.invoke_hook()` — the same surface
the agent loop and cron use), and then checks where the plugin's events land:

1. **Local SQLite sink** — `on_session_start`/`post_api_request` must write the
   run + call rows to `<HERMES_TELEMETRY_HOME>/telemetry/telemetry.db`.
2. **Correlation envelopes** — every hook must produce a redacted, conforming
   envelope in the plugin's ring (`envelope.recent_records()`), carrying the
   board/task/run/branch correlation fields.
3. **Durable transport journal** — `transport.Transport.enqueue()` must fsync
   the envelope to `<HERMES_TELEMETRY_HOME>/telemetry/transport/events.jsonl`
   BEFORE any sink attempt (fail-closed); with no sink configured the event
   stays `pending` and nothing is lost.
4. **Budget gate stays closed OFF** — `pre_tool_call` must NOT block.

Save as `C:/tmp/htc-c2-runbook/capture/event_flow_driver.py`:

```python
"""HTC-C2 event-flow driver — drives the INSTALLED runtime v0.21.3 plugin machinery
against the disposable HERMES_HOME. No network, no credentials, no LLM calls:
hooks are fired through the runtime's own dispatch (hermes_cli.plugins.invoke_hook),
exactly the surface the agent loop uses.

Run with the runtime venv's python:
  HERMES_HOME=... HERMES_TELEMETRY_HOME=... HERMES_TELEMETRY_NO_SETUP=1 \
  python <this file>
"""
import json
import os
import sqlite3
import sys

DISPOSABLE = "C:/tmp/htc-c2-runbook/disposable-home"

# Fail fast if this would ever run against a real home.
# NOTE: paths.py:32 appends /telemetry to the override itself, so BOTH overrides
# point at the home ROOT (a .../telemetry value would nest telemetry/telemetry).
assert os.environ.get("HERMES_HOME") == DISPOSABLE, "HERMES_HOME must point at the disposable home"
assert os.environ.get("HERMES_TELEMETRY_HOME") == DISPOSABLE
assert os.environ.get("HERMES_TELEMETRY_NO_SETUP") == "1"

# --- 1. Load plugins the way the runtime does -------------------------------
from hermes_cli import plugins as hp  # noqa: E402  (installed runtime v0.21.3)

hp.discover_plugins(force=True)
mgr = hp.get_plugin_manager()
loaded = mgr._plugins.get("hermes-telemetry")
print("plugin loaded:", loaded is not None, "| enabled:", bool(loaded and loaded.enabled))
print("hooks registered:", sorted(loaded.hooks_registered))
print("commands registered:", sorted(loaded.commands_registered))
mod = loaded.module

# --- 2. Budget features OFF proof -------------------------------------------
budget_path = mod.paths.get_budget_path()
print("budget.yaml path:", budget_path)
print("budget.yaml exists:", budget_path.exists(), "(absent = budgets disabled — paths.py)")
assert not budget_path.exists()

# --- 3. Fire lifecycle hooks through the runtime dispatch -------------------
SID = "htc-c2-runbook-proof-001"
MODEL = "runbook/disposable-proof"
PROVIDER = "runbook-proof"

r = hp.invoke_hook("on_session_start", session_id=SID, model=MODEL, platform="cli")
print("on_session_start ->", r)
r = hp.invoke_hook("pre_api_request", session_id=SID, api_call_count=1,
                   approx_input_tokens=1200, model=MODEL, provider=PROVIDER, platform="cli")
print("pre_api_request ->", r)
r = hp.invoke_hook("post_api_request", session_id=SID,
                   usage={"input_tokens": 1200, "output_tokens": 340, "cache_read_tokens": 0,
                          "cache_write_tokens": 0, "reasoning_tokens": 0},
                   api_duration=0.421, model=MODEL, provider=PROVIDER, platform="cli",
                   api_call_count=1, assistant_content_chars=1500)
print("post_api_request ->", r)
# Budget gate on the tool path: with budget.yaml absent this MUST NOT block.
r = hp.invoke_hook("pre_tool_call", session_id=SID, tool_name="read_file",
                   tool_call_id="tc_1", task_id="t_none", args={})
print("pre_tool_call ->", r, "(empty => no block; budgets OFF)")
r = hp.invoke_hook("on_session_end", session_id=SID, platform="cli")
print("on_session_end ->", r)
r = hp.invoke_hook("on_session_finalize", session_id=SID)
print("on_session_finalize ->", r)

# --- 4. Correlation envelopes captured by the plugin ------------------------
records = mod.envelope.recent_records()
print("envelope ring records:", len(records))
for rec in records:
    env = rec["envelope"]
    print("  hook=%-20s board=%s task=%s run=%s branch=%s profile=%s call_class=%s"
          % (rec["event"]["hook"], env.get("board"), env.get("task_id"),
             env.get("run_id"), env.get("worktree/branch"),
             env.get("profile/role"), env.get("call_class")))

# --- 5. Durable transport journal: where the integrated transport persists ---
t = mod.transport.Transport()  # default dir = get_transport_dir() = <HERMES_TELEMETRY_HOME>/transport
print("transport dir:", str(t.dir))
env0 = records[0]["envelope"]
receipt = t.enqueue(json.loads(json.dumps(env0)))
print("enqueue receipt:", json.dumps(receipt))
print("transport status:", json.dumps(t.status(), sort_keys=True))
journal = t.journal_path
print("journal exists:", journal.exists(), "| lines:", sum(1 for _ in open(journal)))

# --- 6. Local SQLite sink: what the plugin persisted ------------------------
db_path = mod.paths.get_db_path()
print("telemetry.db:", db_path, "| exists:", db_path.exists())
con = sqlite3.connect(str(db_path))
for row in con.execute("SELECT session_id, model, platform, profile, status FROM runs"):
    print("runs row:", row)
for row in con.execute(
        "SELECT session_id, model, provider, tokens_in, tokens_out, cost_usd, latency_ms FROM llm_calls"):
    print("llm_calls row:", row)
con.close()
print("DRIVER OK")
```

Save the leak-check helper as `C:/tmp/htc-c2-runbook/capture/live_board_leak_check.py`
(read-only row counts on the LIVE board DB — §0.6):

```python
"""HTC-C2-FIX leak check — read-only row counts on the LIVE hermes-telemetry board DB.
Run before and after any probe that exports kanban correlation vars.
Read-only URI mode; never writes the live board."""
import sqlite3

db = r"C:\Users\Sovav\AppData\Local\hermes\kanban\boards\hermes-telemetry\kanban.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("tables:", tables)
for t in tables:
    print(f"count[{t}] =", con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
con.close()
```

Run it (three-pin isolation, §0.6: the correlation exports point at a
non-live board name and a nonexistent disposable DB path; adjust the
task/run/branch values to your run):

```bash
cd C:/tmp/htc-c2-runbook
python capture/live_board_leak_check.py
export HERMES_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_NO_SETUP=1
export HERMES_KANBAN_BOARD=htc-c2-runbook-disposable
export HERMES_KANBAN_DB=C:/tmp/htc-c2-runbook/capture/disposable-kanban.db
export HERMES_KANBAN_TASK=t_9efbcd24
export HERMES_KANBAN_RUN_ID=51
export HERMES_KANBAN_BRANCH=wt/htc-c2-runbook
python capture/event_flow_driver.py
ls capture/disposable-kanban.db 2>/dev/null || echo "ABSENT (probe never touched a board DB)"
python capture/live_board_leak_check.py
```

Expected output (real, driver exit 0; leak-check counts elided to the two rows
that matter for the proof — the full table list is 8 tables, all identical
before/after):

```
count[tasks] = 54            # BEFORE probe
count[task_runs] = 52        # BEFORE probe
hermes-telemetry: no price for model 'runbook/disposable-proof' under provider 'runbook-proof' — cost recorded as $0.00. Add it (under the provider's native model id) to ~/.hermes/telemetry/pricing.yaml to fix.
plugin loaded: True | enabled: True
hooks registered: ['api_request_error', 'on_session_end', 'on_session_finalize', 'on_session_start', 'post_api_request', 'post_llm_call', 'post_tool_call', 'pre_api_request', 'pre_llm_call', 'pre_tool_call', 'subagent_start', 'subagent_stop']
commands registered: ['budget', 'setup', 'stats', 'telemetry-status']
budget.yaml path: C:\tmp\htc-c2-runbook\disposable-home\telemetry\budget.yaml
budget.yaml exists: False (absent = budgets disabled — paths.py)
on_session_start -> []
pre_api_request -> []
post_api_request -> []
pre_tool_call -> [] (empty => no block; budgets OFF)
on_session_end -> []
on_session_finalize -> []
envelope ring records: 6
  hook=on_session_start     board=htc-c2-runbook-disposable task=t_9efbcd24 run=51 branch=wt/htc-c2-runbook profile=default call_class=primary
  hook=pre_api_request      board=htc-c2-runbook-disposable task=t_9efbcd24 run=51 branch=wt/htc-c2-runbook profile=default call_class=primary
  hook=post_api_request     board=htc-c2-runbook-disposable task=t_9efbcd24 run=51 branch=wt/htc-c2-runbook profile=default call_class=primary
  hook=pre_tool_call        board=htc-c2-runbook-disposable task=t_9efbcd24 run=51 branch=wt/htc-c2-runbook profile=default call_class=primary
  hook=on_session_end       board=htc-c2-runbook-disposable task=t_9efbcd24 run=51 branch=wt/htc-c2-runbook profile=default call_class=primary
  hook=on_session_finalize  board=htc-c2-runbook-disposable task=t_9efbcd24 run=51 branch=wt/htc-c2-runbook profile=default call_class=primary
transport dir: C:\tmp\htc-c2-runbook\disposable-home\telemetry\transport
enqueue receipt: {"event_id": "evt_e188d5c4411c48efaa2800f3754ae778", "journal_offset": 0}
transport status: {"cursor_offset": 0, "delivered": 0, "journaled": 1, "last_success_at": null, "oldest_pending_age_seconds": 0.005, "outage_open": false, "pending": 1, "sink_configured": false, "torn_truncations": 0, "transport_dir": "C:\\tmp\\htc-c2-runbook\\disposable-home\\telemetry\\transport"}
journal exists: True | lines: 1
telemetry.db: C:\tmp\htc-c2-runbook\disposable-home\telemetry\telemetry.db | exists: True
runs row: ('htc-c2-runbook-proof-001', 'runbook/disposable-proof', 'cli', 'default', 'ok')
llm_calls row: ('htc-c2-runbook-proof-001', 'runbook/disposable-proof', 'runbook-proof', 1200, 340, 0.0, 421)
DRIVER OK
ABSENT (probe never touched a board DB)
count[tasks] = 54            # AFTER probe — identical
count[task_runs] = 52        # AFTER probe — identical
```

Reading the proof:

- `invoke_hook` returned `[]` for every lifecycle hook — the plugin's callbacks
  ran (they return `None`, which the dispatcher filters) and the budget gate did
  **not** block on `pre_tool_call` (budgets OFF holds on the enforcement path,
  not just the file check).
- 6 envelopes, one per fired hook, each carrying the correlation fields
  (board/task/run/branch) and `call_class=primary`. The board value is the
  **non-live disposable name** and the disposable DB path was never created —
  three-pin isolation held (§0.6).
- The live-board row-count leak check is **identical before and after**
  (`tasks=54`, `task_runs=52`; all 8 tables compared, zero delta) — the probe
  mutated nothing on the live board.
- The transport journal holds the event durably (`journaled: 1, pending: 1,
  sink_configured: false`) — exactly the fail-closed contract: with no sink
  configured, the event is fsynced to `events.jsonl` and waits; nothing is lost,
  nothing is delivered nowhere.
- `telemetry.db` has the session row (`status='ok'`) and the API-call row
  (1200 in / 340 out tokens, `cost_usd=0.0` because the proof model has no
  price entry — the plugin degrades to $0 with a warning, fail-open by design).
- The `$0.00` pricing warning is expected: `runbook/disposable-proof` is not a
  real model. It does not affect the event-flow proof.

---

## 6. Rollback — restore pre-install state, with PROOF

### 6.1 Uninstall via the supported path

```bash
export HERMES_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_HOME=C:/tmp/htc-c2-runbook/disposable-home
export HERMES_TELEMETRY_NO_SETUP=1
hermes plugins remove hermes-telemetry
```

Expected output (real, exit 0):

```
✗ Plugin hermes-telemetry removed from
C:\tmp\htc-c2-runbook\disposable-home\plugins
```

(`✗` is the CLI's remove glyph, not a failure — exit 0, the plugin directory is
gone, and the `plugins.enabled` entry was reverted in `config.yaml`.)

### 6.2 Enumerate what the run left behind

```bash
cd C:/tmp/htc-c2-runbook
find disposable-home -type f | sort | sed 's|disposable-home/||'
```

Expected output (real — **12 residual files**; the count and list include the
`.update_check` stamp the §1 version call wrote under the overrides, which a
previous execution missed):

```
.update_check
backups/config/config.yaml.good.20260919-145540
backups/config/config.yaml.good.20260919-145550
cache/plugin-catalog.json
config.yaml
SOUL.md
telemetry/.pricing_refresh
telemetry/pricing.yaml
telemetry/telemetry.db
telemetry/telemetry.log
telemetry/transport/events.jsonl
telemetry/transport/state.json
```

Every one of these was created after the pre-install inventory was taken
(`plugins/` is already gone after §6.1). Classes:

- `telemetry/*` — plugin state (SQLite DB, durable journal + cursor state,
  pricing cache, log).
- `config.yaml`, `backups/config/*` — runtime config written by enable/remove
  (the runtime keeps its own `.good` backups on each edit; the timestamps
  differ per run).
- `cache/plugin-catalog.json`, `SOUL.md` — runtime home-init artifacts.
- `.update_check` — the runtime's update-check stamp, written by the §1
  `hermes --version` call (under the overrides, so it landed here and not in
  the real home).

### 6.3 Remove them and re-inventory

On a **disposable** home used only for this verification, full restoration means
removing everything the run created — **including `.update_check`**, or the
final inventory will not be identical to §1:

```bash
cd C:/tmp/htc-c2-runbook
rm -rf disposable-home/telemetry disposable-home/backups disposable-home/cache \
       disposable-home/config.yaml disposable-home/SOUL.md disposable-home/.update_check
# The runtime's home-init also created empty skeleton directories; remove them
# so the home is byte-identical to §1:
rmdir disposable-home/audio_cache disposable-home/cron disposable-home/hooks \
      disposable-home/image_cache disposable-home/logs/curator disposable-home/logs \
      disposable-home/memories disposable-home/pairing disposable-home/sessions \
      disposable-home/skills disposable-home/plugins 2>/dev/null
find disposable-home -type f | wc -l
find disposable-home -type f -exec sha256sum {} + | sort > capture/inventory-after.txt
diff capture/inventory-before.txt capture/inventory-after.txt && echo "INVENTORY DIFF: IDENTICAL"
find disposable-home
```

Expected output (real):

```
0
INVENTORY DIFF: IDENTICAL
disposable-home
```

**Rollback proof, stated explicitly:** the full file inventory of the disposable
home was **0 files before install** (§1) and **0 files after rollback** (§6.3);
the SHA-256 manifests `inventory-before.txt` and `inventory-after.txt` both
empty; `diff` reports them **IDENTICAL**, and the home contains no files and no
directories. No plugin code, no telemetry state, no config, no update-check
stamp, and no journal data survived.

### 6.4 Environment rollback — unset every export the runbook made

The runbook exported `HERMES_HOME`, `HERMES_TELEMETRY_HOME`,
`HERMES_TELEMETRY_NO_SETUP` and the §5 kanban-correlation variables. A shell
that keeps them exported will silently pin every later `hermes` command (and
any kanban-touching tool) to the disposable home / disposable board — a review
round 1 blocker. Roll them back and VERIFY:

```bash
unset HERMES_HOME HERMES_TELEMETRY_HOME HERMES_TELEMETRY_NO_SETUP \
      HERMES_KANBAN_BOARD HERMES_KANBAN_DB HERMES_KANBAN_TASK \
      HERMES_KANBAN_RUN_ID HERMES_KANBAN_BRANCH HERMES_KANBAN_PARENT_TASK_IDS
test -z "$HERMES_HOME" && test -z "$HERMES_TELEMETRY_HOME" && \
test -z "$HERMES_TELEMETRY_NO_SETUP" && test -z "$HERMES_KANBAN_DB" && \
test -z "$HERMES_KANBAN_BOARD" && \
echo "ENV ROLLBACK OK: no HERMES / kanban overrides remain exported" || \
echo "ENV ROLLBACK INCOMPLETE"
```

Expected output (real):

```
ENV ROLLBACK OK: no HERMES / kanban overrides remain exported
```

(`unset` is idempotent — variables you did not set are simply still unset.)

### 6.5 Variant for a REAL home (not executed — do not run on `~/.hermes` blindly)

On a home with other state you want to keep, restore only the plugin-scoped
pieces:

1. `hermes plugins remove hermes-telemetry` (reverts `plugins.enabled` and
   deletes `<home>/plugins/hermes-telemetry/`; the runtime's own
   `backups/config/config.yaml.good.*` snapshots bracket every config edit).
2. Delete `<home>/telemetry/` (DB, journal, pricing, log — all plugin-owned;
   `state.db`, `cron/`, `sessions/` etc. are Hermes-core and untouched by this
   plugin, see `paths.py` docstring).
3. Re-run your pre-install inventory diff (§1/§6.3) — everything NOT under
   `plugins/hermes-telemetry/` or `telemetry/` must be unchanged except the
   `config.yaml` plugin entry (verify against the `.good` backup) and the
   `.update_check` stamp (delete it if it did not exist pre-install).

---

## 7. Executed-command index

| # | Step | Command | Result |
|---|------|---------|--------|
| 1 | §1 | `find disposable-home -type f \| wc -l` | `0` |
| 2 | §1 | `find ... -exec sha256sum {} + \| sort > inventory-before.txt` | empty manifest |
| 3 | §1 | exports + `hermes --version` | v0.21.3 (2026.9.14) + `Update available: 780 commits behind`; created `.update_check` + `SOUL.md` in the disposable home |
| 4 | §2 | `pip install --no-deps -e .` | exit 1 — metadata-generation-failed, 0 egg-info (known caveat) |
| 5 | §2 | `git archive 9a66874 --format=tar \| tar -x -C ...` | 123 files installed |
| 6 | §3 | `hermes plugins list --plain --no-bundled` (pre) | `not enabled  user 0.8.0 hermes-telemetry` |
| 7 | §3 | `hermes plugins enable hermes-telemetry` | exit 0, enabled, deps satisfied |
| 8 | §3 | `hermes plugins list --plain --no-bundled` (post) | `enabled  user 0.8.0 hermes-telemetry` |
| 9 | §4 | `test -f .../telemetry/budget.yaml` | ABSENT (budgets disabled) |
| 10 | §5 | `python capture/live_board_leak_check.py` (before) | 8 tables, `tasks=54`, `task_runs=52` |
| 11 | §5 | `python capture/event_flow_driver.py` | exit 0 — 6 envelopes (disposable board), journal `journaled:1 pending:1`, DB rows present, no budget block |
| 12 | §5 | disposable-DB check + `live_board_leak_check.py` (after) | DB ABSENT; counts identical (`tasks=54`, `task_runs=52`) — zero live-board delta |
| 13 | §6.1 | `hermes plugins remove hermes-telemetry` | exit 0, plugin dir gone |
| 14 | §6.2 | `find disposable-home -type f \| sort` | 12 residual files enumerated (incl. `.update_check`) |
| 15 | §6.3 | `rm -rf ...` + `rmdir ...` + re-inventory | `0` files; `diff`: IDENTICAL |
| 16 | §6.4 | `unset ...` + `test -z` guard | `ENV ROLLBACK OK` |

Total: 16 executed commands (plus the driver and leak-check scripts), all with
the real outputs above.
