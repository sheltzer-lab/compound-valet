"""
Centralized prompt building utilities.

This module provides canonical functions for prompt construction to ensure
consistency across every place that builds a prompt.
"""

from typing import List, Dict, Any
from core.db import get_verified_papers


def inject_verified_sources(prompt: str, drug_name: str, job_id: str = None) -> str:
    """
    CANONICAL verified sources injection - used by ALL agents.

    Replaces {{verified_sources}} placeholder with formatted paper list.
    Fetches confirmed papers from the database.

    Args:
        prompt: Prompt template with {{verified_sources}} placeholder
        drug_name: Drug name to load papers for
        job_id: this run's paper scope, when its report is not saved yet. The
            model must see the same papers that will eventually be promoted,
            or the report it writes cites papers the database does not hold.

    Returns:
        Prompt with verified sources injected
    """
    # Always fetch from DB - Source of Truth
    papers = get_verified_papers(drug_name, job_id)

    if not papers:
        # Fallback if DB empty (shouldn't happen if initialized correctly)
        return prompt.replace("{{verified_sources}}", "No verified sources available.")
    
    lines = []
    for i, paper in enumerate(papers, 1):
        title = paper.get('title', 'Unknown')
        year = paper.get('year')
        if not year or str(year).strip().lower() in ['unknown', 'none', 'nan', '']:
            year = '????'
        url = paper.get('url', '#')
        
        # Use simple ID or fetch from map if possible? 
        # Standard ID format: [DrugName-Paper-N]
        paper_id = f"{drug_name}-Paper-{i}"
        
        lines.append(f"{i}. **{title}** ({year}) [{paper_id}]")
        # Surface retractions to the PI (only when confirmed retracted; False /
        # None = checked-clean / unknown are silent, per the tri-state design).
        if paper.get('retracted') is True:
            tag = "   [RETRACTED]"
            if paper.get('retraction_date'):
                tag += f" ({paper['retraction_date']})"
            if paper.get('retraction_notice_doi'):
                tag += f"; retraction notice https://doi.org/{paper['retraction_notice_doi']}"
            lines.append(tag)
        lines.append(f"   Link: [{url}]({url})")
        lines.append("")

    verified_sources_text = "\n".join(lines)
    return prompt.replace("{{verified_sources}}", verified_sources_text)


def format_papers_for_prompt(papers: List[Dict[str, Any]]) -> str:
    """
    Format a list of papers for inclusion in a prompt.
    
    Args:
        papers: List of paper dictionaries with 'authors', 'title', 'year', 'url'
    
    Returns:
        Formatted string suitable for prompt injection
    """
    if not papers:
        return "No papers available."
    
    lines = []
    for i, paper in enumerate(papers, 1):
        title = paper.get('title', 'Unknown')
        year = paper.get('year')
        if not year or str(year).strip().lower() in ['unknown', 'none', 'nan', '']:
            year = '????'
        url = paper.get('url', '#')
        
        lines.append(f"{i}. **{title}** ({year})")
        lines.append(f"   Link: [{url}]({url})")
        lines.append("")
    
    return "\n".join(lines)
