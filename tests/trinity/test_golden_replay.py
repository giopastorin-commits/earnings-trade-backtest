from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import requests

from trinity.shadow.golden_replay import (
    GoldenReplayError,
    canonical_bytes,
    canonical_sha256,
    parse_cutoff,
    prohibit_network,
    run_golden_replay,
)


REFERENCE = Path("tests/golden/shadow_week0")
FROZEN_WEEK0 = Path("data/local/shadow_runs/SHADOW_USA_2026-10-06_W00")


def test_golden_cutoff_is_explicit_and_exact():
    cutoff, session, new_york = parse_cutoff("2026-10-05T20:00:00Z")
    assert cutoff.isoformat() == "2026-10-05T20:00:00+00:00"
    assert session.isoformat() == "2026-10-05"
    assert new_york == "2026-10-05T16:00:00-04:00"
    with pytest.raises(GoldenReplayError, match="explicit UTC"):
        parse_cutoff("2026-10-05T20:00:00")


def test_golden_mode_blocks_network_transports():
    with prohibit_network(), pytest.raises(GoldenReplayError, match="prohibited"):
        requests.get("https://example.invalid", timeout=1)


def test_frozen_reference_hashes_are_self_consistent():
    manifest = json.loads((REFERENCE / "expected_output_manifest.json").read_text())
    funnel = json.loads((REFERENCE / "expected_funnel.json").read_text())
    setups = json.loads((REFERENCE / "expected_setups.json").read_text())
    inputs = json.loads((REFERENCE / "input_manifest.json").read_text())
    assert canonical_sha256(funnel) == manifest["expected_funnel_sha256"]
    assert canonical_sha256(setups) == manifest["expected_setups_sha256"]
    assert inputs["file_count"] == 517
    assert inputs["expected_absent_inputs"] == ["prices/BF.B.json", "prices/BRK.B.json"]
    mapping = REFERENCE / "canonical_ticker_mapping.csv"
    mapping_entry = inputs["files"][0]
    assert mapping.stat().st_size == mapping_entry["byte_length"]
    assert hashlib.sha256(mapping.read_bytes()).hexdigest() == mapping_entry["sha256"]


@pytest.mark.skipif(not FROZEN_WEEK0.is_dir(), reason="local frozen Week 0 artifacts absent")
def test_week0_offline_golden_replay_matches_frozen_artifacts(tmp_path):
    comparison = run_golden_replay(
        decision_cutoff="2026-10-05T20:00:00Z",
        baseline_root=FROZEN_WEEK0,
        universe_path=REFERENCE / "canonical_ticker_mapping.csv",
        output_root=tmp_path / "replay",
    )
    assert comparison["status"] == "MATCH"
    assert comparison["network_calls"] == comparison["model_calls"] == 0
    assert comparison["counts"] == {
        "universe": 518, "valid_ohlcv": 513, "rejected_data_quality": 5,
        "downtrend_rejected": 161, "neutral_survivors": 272,
        "uptrend_survivors": 80, "ready_technically": 33,
        "near": 148, "distant": 171, "final_shortlist_size": 181,
    }
    assert comparison["actual_operational_setups"] == [
        "COP", "CRL", "EXPD", "GILD", "MSFT", "PFE", "WBD",
    ]
    assert all(
        value["result"] == "MATCH"
        for value in comparison["setup_record_hashes"].values()
    )


@pytest.mark.skipif(not FROZEN_WEEK0.is_dir(), reason="local frozen Week 0 artifacts absent")
def test_week0_replay_accepts_compact_versioned_reference(tmp_path):
    comparison = run_golden_replay(
        decision_cutoff="2026-10-05T20:00:00Z",
        baseline_root=FROZEN_WEEK0,
        universe_path=REFERENCE / "canonical_ticker_mapping.csv",
        reference_root=REFERENCE,
        output_root=tmp_path / "compact-reference-replay",
    )
    assert comparison["status"] == "MATCH"
    assert comparison["first_mismatch"] is None
    assert comparison["mismatch_classification"] is None


def test_shadow_production_has_no_codex_cli_transport_dependency():
    forbidden = ("codexcliprovider", "codex exec", "auth.json", "codex_home")
    sources = list(Path("trinity/shadow").glob("*.py"))
    assert sources
    for path in sources:
        lowered = path.read_text(encoding="utf-8").lower()
        for marker in forbidden:
            assert marker not in lowered, f"{path} depends on {marker}"
    workflow = Path(".github/workflows/trinity-shadow-manual.yml").read_text().lower()
    assert all(marker not in workflow for marker in forbidden)


def test_golden_cloud_import_graph_excludes_codex_execution_transport():
    code = (
        "import sys; import trinity.shadow.golden_cloud; "
        "assert 'trinity.italia_real' not in sys.modules; "
        "assert 'trinity.shadow.responses' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_golden_workflow_is_pinned_and_has_no_data_or_model_secrets():
    workflow = Path(".github/workflows/trinity-shadow-manual.yml").read_text()
    golden = workflow.split("  golden-replay:", 1)[1].split("  full-shadow:", 1)[0]
    assert "runs-on: ubuntu-24.04" in golden
    assert 'python-version: "3.13.15"' in golden
    assert "--require-hashes" in golden
    assert "TRINITY_R2_READ_ACCESS_KEY_ID" in golden
    for forbidden in ("OPENAI_API_KEY", "EODHD_API_KEY", "TELEGRAM", "TRINITY_R2_ACCESS_KEY_ID:"):
        assert forbidden not in golden
    for line in (line.strip() for line in golden.splitlines() if "uses:" in line):
        revision = line.rsplit("@", 1)[1]
        assert len(revision) == 40 and all(character in "0123456789abcdef" for character in revision)


def test_canonical_serialization_normalizes_json_container_types():
    assert canonical_bytes({"reason_codes": ("A", "B")}) == canonical_bytes(
        {"reason_codes": ["A", "B"]}
    )
