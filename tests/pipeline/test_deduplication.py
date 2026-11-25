import pytest
from core.utils import is_duplicate

def test_is_duplicate_logic():
    existing_papers = [
        {"title": "PAC-1 is a procaspase-3 activator", "url": "https://pubmed.ncbi.nlm.nih.gov/16936784/", "doi": "10.1038/nchembio814"},
        {"title": "Another paper", "url": "http://example.com", "doi": "10.1234/5678"}
    ]

    # Crypto-duplicate by URL
    dup_url = {"title": "PAC-1 Paper", "url": "https://pubmed.ncbi.nlm.nih.gov/16936784/", "doi": None}
    assert is_duplicate(dup_url, existing_papers) is True

    # Crypto-duplicate by DOI
    dup_doi = {"title": "Different Title", "url": "http://other.com", "doi": "10.1038/nchembio814"}
    assert is_duplicate(dup_doi, existing_papers) is True

    # Crypto-duplicate by Fuzzy Title
    dup_title = {"title": "PAC-1 is a procaspase 3 activator.", "url": "http://new.com", "doi": None}
    assert is_duplicate(dup_title, existing_papers) is True

    # Non-duplicate
    non_dup = {"title": "PAC-1 Clinical Trial", "url": "http://unique.com", "doi": "10.1038/unique"}
    assert is_duplicate(non_dup, existing_papers) is False

def test_deduplication_loop_simulation():
    """Simulates the per-drug harvest dedup loop used by core/ingestion.py
    to ensure is_duplicate is used correctly."""
    papers_from_api = [
        {"title": "Paper A", "url": "http://a.com"},
        {"title": "Paper B", "url": "http://b.com"},
        {"title": "Paper A", "url": "http://a.com"}, # Exact Duplicate
        {"title": "Paper A", "url": "http://d.com"}  # Same title, different URL (should be fuzzy matched)
    ]
    
    current_papers = []
    for p in papers_from_api:
        if not is_duplicate(p, current_papers):
            current_papers.append(p)
            
    assert len(current_papers) == 2
    assert current_papers[0]["title"] == "Paper A"
    assert current_papers[1]["title"] == "Paper B"
