
import os
import time
import logging
import concurrent.futures
from typing import List, Dict, Tuple, Any
import asyncio

from core.references.checker import ReferenceChecker
from core.retraction_live import flag_retractions_live

from .usage import sum_usage

logger = logging.getLogger(__name__)


def run_citation_manager(papers: List[Dict], drug_name: str, verbose: bool = False) -> Tuple[List[Dict], str, int, Dict, Dict, float]:
    """
    Audits a list of papers against Crossref and Semantic Scholar.
    
    Processing:
    1. Uses core.references.checker.ReferenceChecker to verify and correct citations.
    2. Papers with valid metadata and URLs are kept.
    3. Papers that cannot be verified are excluded.
    
    Args:
        papers: List of paper dicts with 'title', 'authors', 'url', etc.
        drug_name: Name of drug being analyzed
        
    Returns:
        - verified_papers: List of papers that passed validation
        - audit_report: Full audit report text
        - score: Citation quality score (0-100)
        - stats: Statistics dict (total, verified, excluded)
        - usage: Empty dict (no longer using LLM directly)
        - duration: Total processing time in seconds
    """
    if not papers:
        return [], "No papers to audit.", 0, {}, {}, 0.0

    start_time = time.time()
    
    # Initialize the checker (Clean Room Implementation)
    try:
        checker = ReferenceChecker()
    except Exception as e:
        logger.error(f"Failed to initialize reference checker: {e}")
        return [], f"Critical Error: Failed to initialize reference checker: {e}", 0, {}, {}, 0.0

    # Build reference list for batch verification
    ref_list = []
    for paper in papers:
        ref_list.append({
            "title": paper.get("title", ""),
            "authors": paper.get("authors", []),
            "year": paper.get("year"),
            "url": paper.get("url"),
            "original_paper_obj": paper # Pass through for easy mapping back
        })

    # Run batch verification
    try:
        # Run async batch verification synchronously
        batch_results = asyncio.run(checker.verify_references_batch(ref_list))
        
        verified_papers = []
        excluded_papers = []
        
        for result in batch_results:
            paper_obj = result.get("reference", {}).get("original_paper_obj")
            if not paper_obj:
                continue
                
            if result.get("verified"):
                # Success! Update paper with verified data
                verified_match = result.get("verified_match", {})
                
                # Start with original object to preserve non-metadata context (IDs, etc.)
                new_paper = paper_obj.copy()
                
                # STRICTLY OVERWRITE core metadata with verified data
                new_paper["title"] = verified_match.get("title")  # Mandatory update
                new_paper["authors"] = verified_match.get("authors", [])
                new_paper["year"] = verified_match.get("year")
                
                # Propagate citation from verified_match with title as final fallback
                citation = verified_match.get("citation")
                if not citation or citation == "Unknown":
                    # Compute from authors/year
                    authors = verified_match.get("authors", [])
                    if isinstance(authors, list) and authors and authors[0]:
                        first_author_last = authors[0].split()[-1]
                    elif isinstance(authors, str) and authors:
                        first_author_last = authors.split(',')[0].strip().split()[-1]
                    else:
                        first_author_last = None
                    
                    year = verified_match.get("year")
                    if first_author_last and year and str(year).lower() not in ['none', 'nan', '']:
                        citation = f"{first_author_last}, {year}"
                    elif first_author_last:
                        citation = first_author_last
                    else:
                        # Final fallback: use title (truncated)
                        title = verified_match.get("title") or "Untitled"
                        citation = title[:50] + "..." if len(title) > 50 else title
                
                new_paper["citation"] = citation
                
                # Add verification details
                new_paper["url"] = result.get("url")
                new_paper["link_valid"] = True
                new_paper["doi"] = verified_match.get("doi")  # for the retraction check

                verified_papers.append(new_paper)
                
                if verbose:
                    print(f"   [Reviewer Source] Verified: {new_paper.get('title')[:50]}... -> {citation}")
            else:
                # Failed
                reason = result.get("error", "Unknown error")
                paper_obj['exclusion_reason'] = reason
                excluded_papers.append(paper_obj)
                
                if verbose:
                    print(f"   [Reviewer Source] ❌ Rejected: {paper_obj.get('title')[:50]}... ({reason})")

        # Flag retractions for the whole batch via a live per-DOI CrossRef read (tri-state).
        asyncio.run(flag_retractions_live(verified_papers))

    except Exception as e:
        logger.error(f"Batch verification failed: {e}")
        return [], f"Batch verification failed: {e}", 0, {}, {}, 0.0

    # Statistics
    total = len(papers)
    verified_count = len(verified_papers)
    excluded_count = len(excluded_papers)
    pass_rate = round(verified_count / total * 100, 1) if total > 0 else 0
    
    stats = {
        "total_citations": total,
        "verified_count": verified_count,
        "excluded_count": excluded_count,
        "pass_rate": pass_rate,
        "excluded_papers": excluded_papers
    }
    
    # Generate audit report
    audit_report = f"""# Citation Manager Audit Report
    
## Summary
- **Total Papers**: {total}
- **Verified**: {verified_count} ({pass_rate}%)
- **Excluded**: {excluded_count}

## Verification Logic
Used `core.references` (Clean Room Implementation) to validate citations against Semantic Scholar and CrossRef with Strict Link Checking.

## Excluded Papers
"""
    if excluded_papers:
        for p in excluded_papers:
            reason = p.get('exclusion_reason', 'Unknown')
            audit_report += f"- {p.get('title', 'Unknown')[:60]}... \n  Reason: {reason}\n"
    else:
        audit_report += "None - all papers passed verification!\n"
    
    score = int(pass_rate)
    duration = time.time() - start_time
    
    logger.info(f"Citation verification for {drug_name}: {verified_count}/{total} papers verified ({pass_rate}%)")
    
    return verified_papers, audit_report, score, stats, {}, duration
