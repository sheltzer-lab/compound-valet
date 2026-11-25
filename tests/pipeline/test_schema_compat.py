import pytest
from core.analysis_schema import build_analysis_result
from core.context import Context

def test_build_analysis_result_compat():
    """
    Verify that build_analysis_result accepts the argument set used by
    the active pipeline callers (core/pipeline.py via simple_batch.py).
    Pins the current schema so refactors that drop a
    kwarg get caught here.
    """
    ctx = Context(drug_name="TestDrug")

    # Must use 'reviewer_reports' (dict), not legacy 'reviewer_report' (str).
    result = build_analysis_result(
        drug_name="TestDrug",
        context=ctx,
        report_text="Report",
        specialist_reports={"genetics": "G", "biochemistry": "B", "biophysics": "P"},
        usage={},
        source="batch_sync",
        model_name="test-model",
        structured_data={},
        time=1.23,
        reviewer_reports={"Reviewer 2": "Critic"}
    )
    
    assert result["drug"] == "TestDrug"
    assert "reviewer_reports" in result
    assert result["reviewer_reports"]["Reviewer 2"] == "Critic"

def test_build_analysis_result_rejects_legacy():
    """
    Verify that passing the legacy 'reviewer_report' argument 
    DOES raise a TypeError. This confirms that if we didn't fix the code,
    it would indeed crash, validating the test's utility.
    """
    ctx = Context(drug_name="TestDrug")
    
    with pytest.raises(TypeError) as excinfo:
        build_analysis_result(
            drug_name="TestDrug",
            context=ctx,
            report_text="Report",
            specialist_reports={},
            usage={},
            reviewer_report="Legacy String" # This MUST fail
        )
    assert "unexpected keyword argument 'reviewer_report'" in str(excinfo.value)
