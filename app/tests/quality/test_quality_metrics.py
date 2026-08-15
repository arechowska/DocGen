from __future__ import annotations

from pathlib import Path

import pytest

from .runner import deterministic_fake_model, evaluate_case

CASES_ROOT = Path(__file__).parent / "cases"
CASE_DIRS = sorted(path for path in CASES_ROOT.iterdir() if path.is_dir())
CASE_IDS = [path.name for path in CASE_DIRS]


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=CASE_IDS)
def test_quality_case_scores_applicable_requirements(case_dir: Path) -> None:
    """Catch lost template sections, gaps, or source grounding in the frozen case."""
    score = evaluate_case(case_dir, deterministic_fake_model())

    assert score.requirement_coverage >= 0.80
    assert score.ungrounded_claims == 0


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=CASE_IDS)
def test_quality_case_finishes_within_its_offline_budget(case_dir: Path) -> None:
    """Catch accidental network/model waits in the deterministic quality runner."""
    score = evaluate_case(case_dir, deterministic_fake_model())

    assert score.processing_seconds <= score.maximum_processing_seconds
