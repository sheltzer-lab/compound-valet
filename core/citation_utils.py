"""
Citation resolution utilities for converting paper IDs to markdown links.
"""

import re
from typing import Dict


def resolve_paper_citations(text: str, paper_id_map: Dict[str, Dict]) -> str:
    """
    Converts [{drug_name}-Paper-1] style citations to [Author, Year](URL) format.
    
    Args:
        text: Report text with [{drug_name}-Paper-N] citations
        paper_id_map: Mapping of paper IDs to paper dicts
        
    Returns:
        Text with resolved citations
        
    Example:
        Input: "Aspirin inhibits COX [Aspirin-Paper-1]."
        Output: "Aspirin inhibits COX [Vane, 1971](https://pubmed.ncbi.nlm.nih.gov/...)."
    """
    if not text or not isinstance(text, str):
        return text if text is not None else ""

    def replace_citation(match):
        paper_id = match.group(1)
        if paper_id in paper_id_map:
            paper = paper_id_map[paper_id]
            # Use pre-computed citation from DB if available
            citation_text = paper.get('citation')
            
            # Fallback if migrating on the fly or old data
            if not citation_text or citation_text == 'Unknown':
                 authors = paper.get('authors', '')
                 first_author = None
                 if isinstance(authors, list):
                    if authors and authors[0]:
                        first_author = authors[0].split()[-1]
                 elif authors and authors != 'Unknown':
                    first_author = authors.split(',')[0].strip().split()[-1]
                 
                 year = paper.get('year', '')
                 if first_author and year:
                    y_str = str(year).strip()
                    if y_str.lower() not in ['none', 'nan', '']:
                        citation_text = f"{first_author}, {y_str}"
                    else:
                        citation_text = first_author
                 elif first_author:
                    citation_text = first_author
                 else:
                    # Final fallback: use title
                    title = paper.get('title') or paper.get('url') or "Untitled"
                    citation_text = title[:50] + "..." if len(title) > 50 else title
            
            url = paper.get('url', '')
            
            return f"[{citation_text}]({url})"
        
        # Keep original match if paper ID not found
        # Note: If we matched brackets in the regex, match.group(0) includes them.
        return match.group(0)
    
    
    # Pre-processing: Ensure comma and space between adjacent citations
    # Matches [A][B] and replaces with [A], [B]
    # Uses lookahead to handle multiple adjacent citations: [A][B][C] -> [A], [B], [C]
    citation_pattern = r'(\[[a-zA-Z0-9\-_]+-Paper-\d+\])(?=\[[a-zA-Z0-9\-_]+-Paper-\d+\])'
    text = re.sub(citation_pattern, r'\1, ', text)

    # Dynamic regex generation to match any valid paper ID from the map
    # This handles spaces, special chars, and ensures we don't pick up random text.
    # We sort by length descending to ensure we find "Longer Name" before "Name" if they share prefixes.
    valid_ids = sorted(paper_id_map.keys(), key=len, reverse=True)
    if not valid_ids:
        return text
        
    escaped_ids = [re.escape(pid) for pid in valid_ids]
    id_pattern = '|'.join(escaped_ids)
    
    # Pattern matches [ID] or ID (unbracketed), optionally matching surrounding backticks to strip them
    # We want to match `[ID]` or `ID` or [ID] or ID.
    # The replacement logic needs to be aware that we might be consuming backticks.
    pattern = r'[`]?\[?(' + id_pattern + r')\]?[`]?'
    
    # We need to ensure we don't partial match inside words if not bracketed?
    # e.g. "Pre-ID" matching "ID".
    # But since we have the specific IDs, it's safer.
    # Let's trust the IDs are unique enough (Drug-Paper-N).
    
    text = re.sub(pattern, replace_citation, text)
    
    # Post-processing: Ensure space between adjacent markdown links
    # Matches patterns like: [Link1](url1)[Link2](url2)
    # Replaces with: [Link1](url1) [Link2](url2)
    text = re.sub(r'(\]\([^\)]+\))(\[)', r'\1 \2', text)
    
    return text


