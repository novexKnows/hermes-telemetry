"""Schema-drift tests for the pinned R-OBS-00 contract artifacts (HTB-B1).

The repo pins byte-identical copies of the R-OBS-00 schema and contract doc;
these tests sha256 the files at TEST TIME and compare against the pinned
constants in envelope.py (TM-verified 2026-09-19 from the R-OBS-00 source).
File bytes are NOT hardcoded — a mismatch or a missing file fails the test,
forcing a deliberate re-pin rather than a silent fork.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SCHEMA_FILE = _ROOT / "schemas" / "correlation-envelope.schema.json"
_CONTRACT_FILE = _ROOT / "docs" / "telemetry" / "telemetry-contract.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_schema_file_exists_and_hash_matches_pin():
    import hermes_telemetry.envelope as envelope

    assert _SCHEMA_FILE.is_file(), f"missing pinned schema copy: {_SCHEMA_FILE}"
    assert _sha256(_SCHEMA_FILE) == envelope.SCHEMA_SHA256


def test_contract_file_exists_and_hash_matches_pin():
    import hermes_telemetry.envelope as envelope

    assert _CONTRACT_FILE.is_file(), f"missing pinned contract copy: {_CONTRACT_FILE}"
    assert _sha256(_CONTRACT_FILE) == envelope.CONTRACT_SHA256


def test_schema_identity_matches_pinned_constants():
    """$id and version inside the pinned copy must match the envelope module's
    constants — a re-pinned file of a different version cannot hide here."""
    import hermes_telemetry.envelope as envelope

    schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    assert schema["$id"] == envelope.SCHEMA_ID
    assert schema["version"] == envelope.SCHEMA_VERSION
    assert envelope.SCHEMA_VERSION == "1.0.0"


def test_schema_is_draft_2020_12_and_forbids_additional_properties():
    schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False


def test_envelope_module_paths_point_at_the_pinned_copy():
    import hermes_telemetry.envelope as envelope

    assert envelope.schema_path() == _SCHEMA_FILE


def test_pinned_copy_is_byte_identical_to_r_obs_00_source():
    """When the R-OBS-00 source tree is present (TM-provided, read-only),
    assert the repo copies are byte-identical to it. Skipped otherwise —
    the sha256 pins above are the offline guarantee."""
    source_dir = Path("C:/tmp/sovav-hermes/r-obs-00/docs/telemetry")
    if not source_dir.is_dir():
        pytest.skip("R-OBS-00 source tree not present on this machine")
    assert _sha256(_SCHEMA_FILE) == _sha256(source_dir / "correlation-envelope.schema.json")
    assert _sha256(_CONTRACT_FILE) == _sha256(source_dir / "telemetry-contract.md")


# ---------------------------------------------------------------------------
# Conformance of constructed envelopes against the pinned schema
# ---------------------------------------------------------------------------


def test_build_envelope_validates_against_pinned_schema():
    jsonschema = pytest.importorskip("jsonschema")
    import hermes_telemetry.envelope as envelope

    schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    for platform in ("cli", "cron", "telegram", ""):
        env = envelope.build_envelope(
            session_id="sess_x",
            model="z-ai/glm-5.3-flash",
            provider="openrouter",
            platform=platform,
            profile="worker",
        )
        errors = list(validator.iter_errors(env))
        assert errors == []
        assert envelope.structural_errors(env) == []


def test_build_envelope_turnstone_originated_form_validates():
    """A repo=turnstone envelope (extension path we never emit from hooks but
    must remain constructible for cross-repo consumers) also validates."""
    jsonschema = pytest.importorskip("jsonschema")
    import hermes_telemetry.envelope as envelope

    schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    env = envelope.build_envelope(session_id="s", profile="worker")
    env["repo"] = "turnstone"
    for field, value in {
        "agent_run_id": "ar_1",
        "correlation_id": "corr_1",
        "idempotency_key": "ar_1:corr_1",
        "deal_id": "deal_1",
        "worker_id": "w_1",
        "model_ref": "openai/gpt-5.6-sol",
        "trigger_type": "manual",
        "runtime_environment": "dev",
        "basis_fingerprint": "bfp_1",
        "worker_definition_version": "1.0.0",
        "output_schema_version": "1.0.0",
    }.items():
        env[field] = value
    env["harness_task_id"] = "t_parent"
    env["harness_run_id"] = "55"
    assert list(validator.iter_errors(env)) == []


def test_structural_errors_catch_missing_base_field():
    import hermes_telemetry.envelope as envelope

    env = envelope.build_envelope(session_id="s", profile="worker")
    del env["trace_id"]
    errors = envelope.structural_errors(env)
    assert any("trace_id" in e for e in errors)


def test_structural_errors_catch_unexpected_field():
    import hermes_telemetry.envelope as envelope

    env = envelope.build_envelope(session_id="s", profile="worker")
    env["prompt"] = "should never be here"
    errors = envelope.structural_errors(env)
    assert any("unexpected field: prompt" in e for e in errors)


def test_structural_errors_catch_bad_call_class():
    import hermes_telemetry.envelope as envelope

    env = envelope.build_envelope(session_id="s", profile="worker", call_class="weird")
    assert any("call_class" in e for e in envelope.structural_errors(env))


def test_structural_errors_catch_non_null_extension_off_turnstone():
    import hermes_telemetry.envelope as envelope

    env = envelope.build_envelope(session_id="s", profile="worker")
    env["agent_run_id"] = "ar_x"
    errors = envelope.structural_errors(env)
    assert any("agent_run_id" in e for e in errors)


def test_harness_pair_consistency_enforced():
    import hermes_telemetry.envelope as envelope

    env = envelope.build_envelope(session_id="s", profile="worker")
    env["harness_task_id"] = "t_x"
    # harness_run_id left null → violates the carried-together rule.
    assert any("harness_" in e for e in envelope.structural_errors(env))
