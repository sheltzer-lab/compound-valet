import pytest
from core.analysis_schema import build_analysis_result
from core.context import Context

def test_build_analysis_result_schema(sample_context):
    """
    Verifies that the builder returns a dict with all expected keys.
    """
    pi_report = "Final PI Report content"
    specialist_reports = {
        "genetics": "Genetics report",
        "biochemistry": "Biochem report",
        "biophysics": "Biophysics report"
    }
    usage = {"prompt_tokens": 100_000, "completion_tokens": 50_000}
    
    result = build_analysis_result(
        drug_name="TestDrug",
        context=sample_context,
        report_text=pi_report,
        specialist_reports=specialist_reports,
        usage=usage,
        source="test",
        model_name="gemini-3.1-pro-preview",
        structured_data={"some": "data"}
    )
    
    # Check top-level keys
    # Check top-level keys
    expected_keys = [
        "drug", "report", "genetics_report", "biochem_report", "biophysics_report",
        "raw_context", "context", "usage", "cost", "time", "timestamp", "source",
        "structured_summary", "mechanism", "reviewer_reports", "citation_score", "citation_stats"
    ]
    for key in expected_keys:
        assert key in result, f"Missing key: {key}"
    
    # Verify legacy key is GONE
    assert "lit_critic_report" not in result, "Legacy key lit_critic_report should be removed"
        
    # Check values
    assert result["drug"] == "TestDrug"
    assert result["report"] == pi_report
    assert result["genetics_report"] == "Genetics report"
    assert result["cost"] > 0
    
    # Verify that harvested_papers exist but URLs are sanitized
    assert "harvested_papers" in result["structured_summary"]
    harvested = result["structured_summary"]["harvested_papers"]
    assert len(harvested) == len(sample_context.relevant_papers)
    # URLs should be redacted to prevent hallucination
    for paper in harvested:
        assert paper["url"] == "[REDACTED - Use Database]"

def test_build_analysis_result_stamps_compound_identity(sample_context):
    """compound_identity is always stamped: resolved=False (no crash) when drug_synonyms is
    empty, populated when a matching row exists."""
    # 1. Empty drug_synonyms -> resolved=False, build still succeeds.
    result = build_analysis_result(
        drug_name="TestDrug", context=sample_context, report_text="r",
        specialist_reports={}, usage={"prompt_tokens": 1, "completion_tokens": 1},
        source="test", model_name="gemini-3.1-pro-preview",
    )
    ci = result["structured_summary"]["compound_identity"]
    assert ci["resolved"] is False
    assert ci["parent_inchikey"] is None

    # 2. With a matching identity row -> populated.
    from core.db import init_db, DrugSynonym
    Session = init_db()
    s = Session()
    try:
        s.add(DrugSynonym(drug_name="TestDrug", status="resolved",
                          parent_inchikey="ABCDEFGHIJKLMN-OPQRSTUVWX-N",
                          cas="50-00-0", chembl_id="CHEMBL123"))
        s.commit()
    finally:
        s.close()
    result2 = build_analysis_result(
        drug_name="TestDrug", context=sample_context, report_text="r",
        specialist_reports={}, usage={"prompt_tokens": 1, "completion_tokens": 1},
        source="test",
    )
    ci2 = result2["structured_summary"]["compound_identity"]
    assert ci2["resolved"] is True
    assert ci2["parent_inchikey"] == "ABCDEFGHIJKLMN-OPQRSTUVWX-N"
    assert ci2["cas"] == "50-00-0" and ci2["chembl_id"] == "CHEMBL123"


def test_build_analysis_result_promotes_mechanism(sample_context):
    """The PI's `mechanism` is promoted to a top-level key for the DB column."""
    result = build_analysis_result(
        drug_name="TestDrug",
        context=sample_context,
        report_text="r",
        specialist_reports={},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        source="test",
        structured_data={"mechanism": "Inhibitor"},
    )
    assert result["mechanism"] == "Inhibitor"

    # Absent mechanism -> None (key still present).
    result2 = build_analysis_result(
        drug_name="TestDrug2",
        context=sample_context,
        report_text="r",
        specialist_reports={},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        source="test",
        structured_data={"target": "TYMS"},
    )
    assert result2["mechanism"] is None


def test_unverified_forces_mechanism_unknown(sample_context):
    """An UNVERIFIED verdict must force mechanism to Unknown, in both the promoted
    field and structured_summary — whatever the PI/biochem proposed."""
    result = build_analysis_result(
        drug_name="TestDrug", context=sample_context, report_text="r",
        specialist_reports={}, usage={"prompt_tokens": 1, "completion_tokens": 1},
        source="test",
        structured_data={"final_verdict": "UNVERIFIED", "mechanism": "Degrader"},
    )
    assert result["mechanism"] == "Unknown"
    assert result["structured_summary"]["mechanism"] == "Unknown"

    # VERIFIED is left alone — guards the substring trap ("UNVERIFIED" contains "VERIFIED").
    verified = build_analysis_result(
        drug_name="TestDrug2", context=sample_context, report_text="r",
        specialist_reports={}, usage={"prompt_tokens": 1, "completion_tokens": 1},
        source="test",
        structured_data={"final_verdict": "VERIFIED", "mechanism": "Degrader"},
    )
    assert verified["mechanism"] == "Degrader"


def test_build_analysis_result_missing_optional(sample_context):
    """
    Verifies behavior when optional fields like structured_data are missing.
    """
    result = build_analysis_result(
        drug_name="TestDrug",
        context=sample_context,
        report_text="Report for missing data test",
        specialist_reports={},
        usage={},
        source="test"
    )
    
    # Should automatically inject harvest papers into structured summary
    assert "harvested_papers" in result["structured_summary"]
    # Should handle empty usage (cost = 0)
    assert result["cost"] == 0.0
