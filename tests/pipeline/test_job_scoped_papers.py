"""A run whose report is not saved must not touch verified_papers.

The bug these pin: an unsaved run wrote its papers straight
into verified_papers at ingestion (which deletes the drug's existing rows
first), while the report was only written if the run was saved. When it was
not, the drug kept its OLD report and its NEW papers, so the report cited
papers the database no longer held.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import (  # noqa: E402
    delete_analysis,
    discard_job_papers,
    get_analysis,
    get_verified_papers,
    promote_job_papers,
    save_papers_standalone,
)

LIVE = [{"title": "Live paper", "authors": ["A"], "year": "2020",
         "url": "https://example.org/live", "citation": "A, 2020"}]
FRESH = [{"title": "Fresh paper", "authors": ["B"], "year": "2024",
          "url": "https://example.org/fresh", "citation": "B, 2024"},
         {"title": "Second fresh", "authors": ["C"], "year": "2025",
          "url": "https://example.org/fresh2", "citation": "C, 2025"}]


def _report(text, sources=1):
    """A result dict. `sources` is how many papers the agents were shown —
    promote reads that off raw_context to tell "this run found nothing" from
    "this job's rows are gone"."""
    ctx = "\n".join(f"### {i}. Paper ({i})\n[Source](https://example.org/p{i})"
                    for i in range(1, sources + 1)) or "No specific literature found."
    return {"report": text, "structured_summary": {},
            "raw_context": f"# Drug Context\n## Key Literature Evidence\n{ctx}"}


@pytest.fixture
def drug():
    name = "TestDrug_JobScoped"
    delete_analysis(name)
    save_papers_standalone(name, [])
    yield name
    delete_analysis(name)
    save_papers_standalone(name, [])
    discard_job_papers("job-1")
    discard_job_papers("job-2")


def test_unsaved_run_leaves_live_papers_untouched(drug):
    """The whole bug in one assertion: job scoping must not disturb the live rows."""
    save_papers_standalone(drug, LIVE)
    promote_job_papers(drug, _report("old report"))

    # A rerun that is never saved.
    save_papers_standalone(drug, FRESH, job_id="job-1")

    live = get_verified_papers(drug)
    assert [p["url"] for p in live] == ["https://example.org/live"]
    scoped = get_verified_papers(drug, job_id="job-1")
    assert [p["url"] for p in scoped] == ["https://example.org/fresh",
                                          "https://example.org/fresh2"]
    assert get_analysis(drug)["report"] == "old report"


def test_promote_moves_papers_and_report_together(drug):
    save_papers_standalone(drug, LIVE)
    promote_job_papers(drug, _report("old report"))
    save_papers_standalone(drug, FRESH, job_id="job-1")

    promote_job_papers(drug, _report("new report"), job_id="job-1")

    live = get_verified_papers(drug)
    assert [p["url"] for p in live] == ["https://example.org/fresh",
                                        "https://example.org/fresh2"]
    assert get_analysis(drug)["report"] == "new report"
    # The job-scoped rows are consumed, so a replay cannot apply them twice.
    assert get_verified_papers(drug, job_id="job-1") == []


def test_two_runs_on_one_drug_do_not_mix(drug):
    """Concurrent runs of the same drug must stay in their own scopes."""
    save_papers_standalone(drug, FRESH, job_id="job-1")
    save_papers_standalone(drug, LIVE, job_id="job-2")

    assert len(get_verified_papers(drug, job_id="job-1")) == 2
    assert len(get_verified_papers(drug, job_id="job-2")) == 1
    assert get_verified_papers(drug) == []


def test_promote_without_scoped_rows_keeps_papers_the_report_cites(drug):
    """Rows already promoted or swept, but the report cites papers: keep them.

    Replacing a good paper set with nothing is the damage this module prevents.
    """
    save_papers_standalone(drug, LIVE)
    promote_job_papers(drug, _report("legacy report", sources=3), job_id="job-gone")

    assert [p["url"] for p in get_verified_papers(drug)] == ["https://example.org/live"]
    assert get_analysis(drug)["report"] == "legacy report"


def test_promote_of_a_run_that_found_nothing_clears_the_papers(drug):
    """The opposite case, seen on ADS 815EI: the run verified zero papers.

    Its report says there is no verified literature, so leaving the drug's old
    papers in place would pair that report with papers it never saw. The empty
    set is the consistent answer.
    """
    save_papers_standalone(drug, LIVE)
    promote_job_papers(drug, _report("old report", sources=1))
    assert len(get_verified_papers(drug)) == 1

    promote_job_papers(drug, _report("no literature found", sources=0),
                       job_id="job-empty")

    assert get_verified_papers(drug) == []
    assert get_analysis(drug)["report"] == "no literature found"


def test_discard_drops_only_that_job(drug):
    save_papers_standalone(drug, FRESH, job_id="job-1")
    save_papers_standalone(drug, LIVE, job_id="job-2")

    assert discard_job_papers("job-1") == 2
    assert get_verified_papers(drug, job_id="job-1") == []
    assert len(get_verified_papers(drug, job_id="job-2")) == 1


def test_empty_run_detection_matches_what_the_context_actually_emits():
    """_run_found_no_papers greps raw_context for a string core/context.py
    writes. Pin the coupling: if get_agent_context changes its format, the
    empty-run branch silently stops firing and the ADS 815EI case comes back."""
    from core.context import Context
    from core.db import _run_found_no_papers

    empty = Context(drug_name="X", relevant_papers=[])
    assert _run_found_no_papers({"raw_context": empty.get_agent_context()}) is True

    one = Context(drug_name="X", relevant_papers=[
        {"title": "T", "year": "2024", "url": "https://example.org/p"}])
    assert _run_found_no_papers({"raw_context": one.get_agent_context()}) is False


def test_unscoped_calls_are_unchanged(drug):
    """job_id=None must behave exactly as before this table existed."""
    save_papers_standalone(drug, LIVE)
    assert [p["url"] for p in get_verified_papers(drug)] == ["https://example.org/live"]
    save_papers_standalone(drug, FRESH)
    assert len(get_verified_papers(drug)) == 2
