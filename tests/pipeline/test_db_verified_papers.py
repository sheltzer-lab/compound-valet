"""
Test to ensure save_analysis does not overwrite verified_papers with redacted URLs.

This test verifies that the verified_papers table maintains real URLs even after
save_analysis is called with sanitized papers in structured_summary.
"""

import pytest
import os
import sys
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import (
    init_db, 
    save_papers_standalone, 
    get_verified_papers, 
    save_analysis,
    delete_analysis
)


def test_save_analysis_does_not_overwrite_verified_papers():
    """
    Test that save_analysis does not overwrite verified_papers with redacted URLs.
    
    Flow:
    1. Save papers with real URLs via save_papers_standalone (simulating ingestion)
    2. Call save_analysis with sanitized papers in structured_summary (simulating final save)
    3. Verify that get_verified_papers still returns real URLs (not redacted)
    """
    drug_name = "TestDrug_VerifiedPapers"
    
    # Cleanup any existing data
    delete_analysis(drug_name)
    
    # Step 1: Save papers with REAL URLs (as done during ingestion)
    real_papers = [
        {
            "title": "Paper 1",
            "authors": ["Smith, J.", "Doe, A."],
            "year": "2023",
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345678"
        },
        {
            "title": "Paper 2",
            "authors": ["Johnson, B."],
            "year": "2024",
            "url": "https://www.nature.com/articles/s41586-024-12345"
        }
    ]
    
    save_papers_standalone(drug_name, real_papers)
    
    # Verify papers were saved with real URLs
    papers_after_ingestion = get_verified_papers(drug_name)
    assert len(papers_after_ingestion) == 2
    assert papers_after_ingestion[0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678"
    assert papers_after_ingestion[1]["url"] == "https://www.nature.com/articles/s41586-024-12345"
    
    # Step 2: Call save_analysis with SANITIZED papers in structured_summary
    # (This simulates what happens when build_analysis_result creates the final result)
    sanitized_papers = [
        {
            "title": "Paper 1",
            "authors": ["Smith, J.", "Doe, A."],
            "year": "2023",
            "url": "[REDACTED - Use Database]"  # Sanitized URL
        },
        {
            "title": "Paper 2",
            "authors": ["Johnson, B."],
            "year": "2024",
            "url": "[REDACTED - Use Database]"  # Sanitized URL
        }
    ]
    
    analysis_data = {
        "drug": drug_name,
        "report": "Test report",
        "genetics_report": "Genetics test",
        "biochem_report": "Biochem test",
        "biophysics_report": "Biophysics test",
        "raw_context": "Test context",
        "context": "Test summary",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        "cost": 0.001,
        "time": 10.5,
        "structured_summary": {
            "harvested_papers": sanitized_papers,  # Contains redacted URLs
            "final_verdict": "VALID"
        },
        "reviewer_reports": {"Reviewer": "Critic report"},
        "citation_score": 95,
        "citation_stats": {"total": 2, "verified": 2}
    }
    
    save_analysis(drug_name, analysis_data)
    
    # Step 3: Verify that verified_papers STILL has real URLs (not overwritten)
    papers_after_save = get_verified_papers(drug_name)
    
    assert len(papers_after_save) == 2, "Should still have 2 papers"
    
    # CRITICAL: URLs should NOT be redacted
    assert papers_after_save[0]["url"] != "[REDACTED - Use Database]", \
        "URL should not be redacted after save_analysis"
    assert papers_after_save[1]["url"] != "[REDACTED - Use Database]", \
        "URL should not be redacted after save_analysis"
    
    # URLs should still be the original real URLs
    assert papers_after_save[0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678", \
        "URL should remain unchanged after save_analysis"
    assert papers_after_save[1]["url"] == "https://www.nature.com/articles/s41586-024-12345", \
        "URL should remain unchanged after save_analysis"
    
    # Cleanup
    delete_analysis(drug_name)
    
    print("✅ Test passed: save_analysis does not overwrite verified_papers with redacted URLs")


def test_verified_papers_persist_across_analysis_updates():
    """
    Test that verified papers persist correctly when analysis is updated multiple times.
    """
    drug_name = "TestDrug_Persistence"
    
    # Cleanup
    delete_analysis(drug_name)
    
    # Save papers
    papers = [
        {
            "title": "Persistence Test Paper",
            "authors": ["Author, A."],
            "year": "2024",
            "url": "https://example.com/paper123"
        }
    ]
    
    save_papers_standalone(drug_name, papers)
    
    # First save_analysis
    analysis_data_v1 = {
        "drug": drug_name,
        "report": "Version 1",
        "usage": {},
        "cost": 0.0,
        "time": 1.0,
        "structured_summary": {
            "harvested_papers": [{"title": "Persistence Test Paper", "url": "[REDACTED - Use Database]"}]
        }
    }
    save_analysis(drug_name, analysis_data_v1)
    
    # Verify URL is still real
    papers_v1 = get_verified_papers(drug_name)
    assert papers_v1[0]["url"] == "https://example.com/paper123"
    
    # Second save_analysis (update)
    analysis_data_v2 = {
        "drug": drug_name,
        "report": "Version 2",
        "usage": {},
        "cost": 0.0,
        "time": 2.0,
        "structured_summary": {
            "harvested_papers": [{"title": "Persistence Test Paper", "url": "[REDACTED - Use Database]"}]
        }
    }
    save_analysis(drug_name, analysis_data_v2)
    
    # Verify URL is STILL real after update
    papers_v2 = get_verified_papers(drug_name)
    assert papers_v2[0]["url"] == "https://example.com/paper123"
    
    # Cleanup
    delete_analysis(drug_name)
    
    print("✅ Test passed: verified papers persist across analysis updates")


def test_doi_round_trips():
    """A DOI resolved during analysis survives save -> read."""
    drug_name = "TestDrug_DoiRoundTrip"
    delete_analysis(drug_name)

    save_papers_standalone(drug_name, [
        {
            "title": "Paper With DOI",
            "authors": ["Smith, J."],
            "year": "2023",
            "url": "https://doi.org/10.1038/s41586-023-12345",
            "doi": "10.1038/s41586-023-12345",
        },
        {
            "title": "Paper Without DOI",
            "authors": ["Doe, A."],
            "year": "2024",
            "url": "https://example.com/no-doi-here",
        },
    ])

    papers = get_verified_papers(drug_name)
    assert papers[0]["doi"] == "10.1038/s41586-023-12345"
    # Absent, not empty string: NULL means the DOI did not resolve.
    assert papers[1]["doi"] is None

    delete_analysis(drug_name)


def test_doi_survives_read_modify_write_cycle():
    """Regression: the missing-papers merge in core/pipeline.py reads rows back with
    get_verified_papers, appends to that list, and re-saves the whole thing — and
    save_verified_papers deletes before reinserting. When get_verified_papers
    hardcoded doi=None, that round-trip silently blanked the column for the drug.
    """
    drug_name = "TestDrug_DoiRoundTripMerge"
    delete_analysis(drug_name)

    save_papers_standalone(drug_name, [{
        "title": "Original Paper",
        "authors": ["Smith, J."],
        "year": "2023",
        "url": "https://doi.org/10.1038/s41586-023-12345",
        "doi": "10.1038/s41586-023-12345",
    }])

    # Exactly what core/pipeline.py does: read back, append, re-save the merged list.
    merged = list(get_verified_papers(drug_name))
    merged.append({
        "title": "Reviewer-Suggested Paper",
        "authors": ["Doe, A."],
        "year": "2024",
        "url": "https://doi.org/10.1016/j.cell.2024.99999",
        "doi": "10.1016/j.cell.2024.99999",
    })
    save_papers_standalone(drug_name, merged)

    after = {p["title"]: p["doi"] for p in get_verified_papers(drug_name)}
    assert after["Original Paper"] == "10.1038/s41586-023-12345", \
        "pre-existing DOI must survive the read-modify-write cycle"
    assert after["Reviewer-Suggested Paper"] == "10.1016/j.cell.2024.99999"

    delete_analysis(drug_name)


if __name__ == "__main__":
    test_save_analysis_does_not_overwrite_verified_papers()
    test_verified_papers_persist_across_analysis_updates()
    test_doi_round_trips()
    test_doi_survives_read_modify_write_cycle()
    print("\n✅ All tests passed!")
