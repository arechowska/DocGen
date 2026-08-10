from __future__ import annotations

from pathlib import Path

from .runner import deterministic_fake_model, evaluate_case

CASE_DIR = Path(__file__).parent / "cases" / "use-case-basic"


def test_quality_case_scores_applicable_requirements() -> None:
    """Catch lost template sections, gaps, or source grounding in the frozen case."""
    score = evaluate_case(CASE_DIR, deterministic_fake_model())

    assert score.requirement_coverage >= 0.80
    assert score.ungrounded_claims == 0


def test_quality_case_finishes_within_its_offline_budget() -> None:
    """Catch accidental network/model waits in the deterministic quality runner."""
    score = evaluate_case(CASE_DIR, deterministic_fake_model())

    assert score.processing_seconds <= score.maximum_processing_seconds
