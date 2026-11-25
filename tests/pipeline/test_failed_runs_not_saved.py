"""A run whose Gemini calls fail is not saved: it raises, and the batch counts it as failed.

Two cases fail a run: every literature search's call failed, or neither the PI draft nor the
PI revision gave a report. One failed search, or a failed draft that the revision replaces,
does not.
"""
from unittest.mock import MagicMock, patch

import pytest

from core.context import Context

FAILED_CALL = {"text": "Error communicating with Gemini API: 401", "usage": {}, "error": True}
NO_CITATIONS = ([], "", 0, {}, {}, 0.0)


def test_all_literature_searches_failing_fails_the_run():
    from core.ingestion import build_initial_context
    checker = MagicMock(return_value=NO_CITATIONS)
    with patch("core.ingestion.generate_with_search", return_value=FAILED_CALL), \
         patch("core.ingestion.run_citation_manager", checker):
        with pytest.raises(RuntimeError, match="literature searches failed"):
            build_initial_context("TestDrug")
    checker.assert_not_called()


def test_one_failed_search_does_not_fail_the_run():
    from core.ingestion import build_initial_context
    answers = iter([FAILED_CALL, {"text": '{"papers": []}', "usage": {}}, {"text": '{"papers": []}', "usage": {}}])
    with patch("core.ingestion.generate_with_search", side_effect=lambda prompt: next(answers)), \
         patch("core.ingestion.run_citation_manager", return_value=NO_CITATIONS):
        ctx = build_initial_context("TestDrug")
    assert ctx.relevant_papers == []


def _run_with_pi(draft, revised):
    """Run the pipeline (auto_save) with every model call mocked; the PI gives `draft` and the
    revision gives `revised`."""
    from core.pipeline import run_analysis_pipeline
    ctx = MagicMock(spec=Context)
    ctx.drug_name = "TestDrug"
    ctx.relevant_papers = []
    ctx.verified_paper_ids = {}
    ctx.usage_metadata = {}
    ctx.get_agent_context.return_value = ""
    ctx.summary.return_value = ""
    with patch("core.pipeline.build_initial_context", return_value=ctx), \
         patch("core.db.get_verified_papers", return_value=[]), \
         patch("core.pipeline.generate_genetics_report", return_value="Genetics"), \
         patch("core.pipeline.generate_biochemistry_report", return_value="Biochem"), \
         patch("core.pipeline.generate_biophysics_report", return_value="Biophysics"), \
         patch("core.pipeline.generate_pi_report", return_value=draft), \
         patch("core.pipeline.resolve_identity_from_report", side_effect=RuntimeError("no index")), \
         patch("core.pipeline.generate_reviewer_report", side_effect=lambda name, *a, **k: (name, "Review")), \
         patch("core.pipeline.revise_pi_report", return_value=revised):
        return run_analysis_pipeline("TestDrug", auto_save=True)


def test_no_pi_report_fails_the_run_and_saves_nothing():
    from core.db import get_analysis
    with pytest.raises(RuntimeError, match="No PI report"):
        _run_with_pi(("Error: Failed to generate PI report.", {}), None)
    assert get_analysis("TestDrug") is None


def test_a_revision_after_a_failed_draft_is_saved():
    from core.db import get_analysis
    _run_with_pi(("Error: Failed to generate PI report.", {}),
                 ("Final report", {"final_verdict": "VERIFIED", "target": "ABL1"}))
    assert get_analysis("TestDrug")["structured_summary"]["final_verdict"] == "VERIFIED"
