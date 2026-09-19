"""Isolation contract — the test suite must never read or write the developer's
real ~/.hermes. HERMES_HOME is the single source of truth for locating Hermes
files; no code path may fall back to Path.home()/.hermes when HERMES_HOME is set.

These tests fail-closed: they plant a POISONED file at Path.home()/.hermes and
assert it is never consulted. They deliberately patch Path.home() directly, so
they catch escapes even though the conftest baseline also pins HOME as a safety
net (otherwise the net would mask the very bug we want to detect).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import hermes_telemetry.budget as budget
import hermes_telemetry.db as db


def test_path_helpers_resolve_under_hermes_home(tmp_path, monkeypatch):
    """The path helpers must derive from HERMES_HOME (or the pinned
    HERMES_TELEMETRY_HOME), never Path.home()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "telemetry-home"))
    tele = tmp_path / "telemetry-home" / "telemetry"
    assert budget._budget_path() == tele / "budget.yaml"
    assert budget._pricing_path() == tele / "pricing.yaml"


def test_budget_status_ignores_real_home_pricing(tmp_path, monkeypatch):
    """Regression for the Path.home() escape in _status_block: the estimated-
    price warning must read pricing.yaml from HERMES_HOME, never Path.home().

    HERMES_HOME gets a clean pricing.yaml (no estimated-price models); a decoy
    "real home" gets a poisoned one (with estimated-price models). If any code
    reads Path.home(), the warning surfaces and this test fails.
    """
    # HERMES_HOME → clean pricing.yaml
    hh = tmp_path / "hermes_home" / "telemetry"
    hh.mkdir(parents=True)
    (hh / "pricing.yaml").write_text("models: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))

    # Decoy "real home" → poisoned pricing.yaml (has estimated-price models)
    decoy_home = tmp_path / "decoy_home"
    decoy_tele = decoy_home / ".hermes" / "telemetry"
    decoy_tele.mkdir(parents=True)
    (decoy_tele / "pricing.yaml").write_text(
        textwrap.dedent(
            """
            models: {}
            _meta:
              estimated_price_models:
              - poison/should-never-be-read
            """
        )
    )
    monkeypatch.setattr(Path, "home", lambda: decoy_home)

    db._local.conn = None
    budget.reload_config()
    out = budget._status_block()

    # If _status_block read Path.home() (the decoy), the estimated-price warning
    # would appear. With HERMES_HOME clean, it must not.
    assert "estimated pricing" not in out, (
        "budget._status_block() read pricing.yaml from Path.home() instead of "
        "HERMES_HOME — real-home isolation is broken"
    )


def test_conftest_pins_ambient_telemetry_home(tmp_path):
    """The autouse isolation fixture must PIN HERMES_TELEMETRY_HOME to a
    disposable root (not merely delete it): paths.py gives HERMES_TELEMETRY_HOME
    priority over HERMES_HOME, so deletion alone would let an ambient value
    re-set later (or a missed fixture layer) leak the suite into the real
    telemetry dir. The pinned value must live inside the per-test tmp dir and
    must never be the developer's real telemetry home."""
    import os

    val = os.environ.get("HERMES_TELEMETRY_HOME")
    assert val is not None, "HERMES_TELEMETRY_HOME must be pinned, not left ambient"
    assert val.startswith(str(tmp_path)), (
        f"HERMES_TELEMETRY_HOME must resolve inside the disposable tmp root, got {val}"
    )


def test_telemetry_home_precedence_stays_in_tmp(tmp_path):
    """With the fixture active, telemetry resolves under the pinned disposable
    HERMES_TELEMETRY_HOME — never the developer's real ~/.hermes/telemetry."""
    import os

    import hermes_telemetry.paths as paths

    expected = Path(os.environ["HERMES_TELEMETRY_HOME"]) / "telemetry"
    assert paths.get_telemetry_home() == expected
    assert expected.is_relative_to(tmp_path), (
        f"telemetry home must resolve inside the disposable tmp root, got {expected}"
    )


def test_budget_paths_honor_telemetry_home(tmp_path, monkeypatch):
    """budget._budget_path / _pricing_path route through the canonical home."""
    import hermes_telemetry.budget as budget

    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    tele = tmp_path / "shared" / "telemetry"
    assert budget._budget_path() == tele / "budget.yaml"
    assert budget._pricing_path() == tele / "pricing.yaml"
