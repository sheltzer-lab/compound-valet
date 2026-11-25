"""
Utility functions for Reference Checker
Including text normalization, author matching, and URL safety checks.
"""

import re
import unicodedata
import logging
import urllib.request
import json
from typing import List, Optional, Tuple, Dict

logger = logging.getLogger(__name__)

# --- Text Normalization Utils ---

def normalize_text(text: str) -> str:
    """Normalize text by removing diacritical marks and special characters"""
    if not text:
        return ""
        
    text = unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII')
    text = re.sub(r"[^\w\s]", '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text.lower()

def clean_title_for_search(title: str) -> str:
    """Clean title for API search queries"""
    if not title:
        return ""
    title = title.replace('\n', ' ').strip()
    title = re.sub(r'\s+', ' ', title)
    return title



# --- URL Safety Utils ---

def is_binding_db_link(url: str) -> bool:
    """Check if URL points to a blocked domain"""
    if not url:
        return False
        
    url_lower = url.lower()
    blocked_keywords = [
        'bindingdb.org',
        'ebi.ac.uk/chembl',
        'pubchem.ncbi.nlm.nih.gov',
        'uniprot.org',
        'rcsb.org',
        'wwpdb.org',
        'drugbank.com',
        'drugbank.ca',
        'semanticscholar.org'  
    ]
    
    for keyword in blocked_keywords:
        if keyword in url_lower:
            return True

    # Block specific DOI patterns that resolve to PDB
    if 'doi.org' in url_lower and url_lower.endswith('/pdb'):
        return True

    return False

def resolve_doi_via_crossref_json(doi_url: str) -> str:
    """Resolve DOI to target URL using CrossRef JSON"""
    if not doi_url or 'doi.org' not in doi_url:
        return doi_url

    try:
        from core.references.clients import CROSSREF_MAILTO
        headers = {
            'Accept': 'application/vnd.citationstyles.csl+json',
            # Polite pool — see CROSSREF_MAILTO in core/references/clients.py.
            'User-Agent': 'CompoundVALET/1.0' + (f' (mailto:{CROSSREF_MAILTO})' if CROSSREF_MAILTO else '')
        }
        req = urllib.request.Request(doi_url, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                data = json.loads(response.read().decode())
                if 'URL' in data:
                    return data['URL']
    except Exception:
        pass
    return doi_url

def is_safe_url(url: str) -> bool:
    """Check if URL is safe: valid format, not blocked, resolves safely"""
    if not url or not url.startswith(('http://', 'https://')):
        return False
        
    if is_binding_db_link(url):
        return False
        
    if 'doi.org' in url:
        resolved_url = resolve_doi_via_crossref_json(url)
        if resolved_url != url and is_binding_db_link(resolved_url):
            return False
            
    return True
