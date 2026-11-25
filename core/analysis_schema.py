from typing import Dict, Any, Optional, List
from datetime import datetime
from core.billing import calculate_cost
from core.context import Context

def sanitize_papers_for_summary(papers: List[Dict]) -> List[Dict]:
    """
    Remove URLs from papers to prevent hallucination from structured_summary.
    The database VerifiedPaper table is the single source of truth for URLs.
    
    Args:
        papers: List of paper dictionaries with URLs
        
    Returns:
        List of paper dictionaries with URLs redacted
    """
    sanitized = []
    for paper in papers:
        clean_paper = paper.copy()
        # Redact URL to prevent any code from using structured_summary as a source
        clean_paper['url'] = '[REDACTED - Use Database]'
        sanitized.append(clean_paper)
    return sanitized

def build_analysis_result(
    drug_name: str,
    context: Context,
    report_text: str,
    specialist_reports: Dict[str, str], # {"genetics": "...", ...}
    usage: Dict[str, int],
    source: str = "batch",
    model_name: Optional[str] = None,
    structured_data: Optional[Dict] = None,
    time: float = 0.0,
    reviewer_reports: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Constructs the standardized analysis result dictionary for saving to the database.
    Every run saves exactly the same fields.
    
    Args:
        drug_name: Name of the drug
        context: The Context object containing papers, audit logs, etc.
        report_text: The final PI report text
        specialist_reports: Dictionary of specialist report strings
        usage: Aggregated usage dictionary
        source: Identifier for where this run came from
        model_name: Used for cost calculation
        structured_data: Optional structured data from PI (e.g. JSON output)
        reviewer_reports: Optional dict of reviewer name -> review text

    Returns:
        Dictionary ready to be passed to `save_analysis`
    """
    
    # 1. Calculate Cost
    cost = calculate_cost(usage, model_name=model_name)
    
    # 2. Ensure structured data has harvested papers (SANITIZED - no URLs)
    if structured_data is None:
        structured_data = {}
        
    # Always ensure the papers are in the structured summary, but strip URLs
    # to prevent hallucination. The database is the single source of truth.
    if "harvested_papers" not in structured_data or not structured_data["harvested_papers"]:
        structured_data["harvested_papers"] = sanitize_papers_for_summary(context.relevant_papers)

    # 2a. An UNVERIFIED mechanism cannot carry a determinate mechanism class. The PI prompt
    # states this rule, but enforce it here too so structured_summary and the promoted
    # `mechanism` column can never disagree with the verdict.
    if "UNVERIFIED" in str(structured_data.get("final_verdict") or "").upper():
        structured_data["mechanism"] = "Unknown"

    # 2b. Stamp the structure-anchored compound identity (canonical InChIKey + cross-refs) into
    # the summary so the saved report carries it. Best-effort and DB-only: get_identity degrades
    # to None when the drug_synonyms table is absent, recorded here as resolved=False — never
    # breaks report building.
    try:
        from core.identity import get_identity
        ident = get_identity(drug_name) or {}
    except Exception:
        ident = {}
    structured_data["compound_identity"] = {
        "parent_inchikey": ident.get("parent_inchikey"),
        "inchikey": ident.get("inchikey"),
        "cid": ident.get("cid"),
        "formula": ident.get("formula"),
        "iupac_name": ident.get("iupac_name"),
        "pubchem_title": ident.get("pubchem_title"),
        "cas": ident.get("cas"),
        "chembl_id": ident.get("chembl_id"),
        "drugbank_id": ident.get("drugbank_id"),
        "unii": ident.get("unii"),
        "chebi_id": ident.get("chebi_id"),
        "drugcentral_id": ident.get("drugcentral_id"),
        "status": ident.get("status"),
        "resolved": bool(ident),
    }

    # 3. Construct Result
    return {
        "drug": drug_name,
        "report": report_text, # Main PI Report

        # Reports
        "genetics_report": specialist_reports.get("genetics"),
        "biochem_report": specialist_reports.get("biochemistry"),
        "biophysics_report": specialist_reports.get("biophysics"),

        "reviewer_reports": reviewer_reports, # New Dict {Name: Text}

        # Context & Debug
        "raw_context": context.get_agent_context(), # What the agents saw
        "context": context.summary(), # Human readable summary

        # Metrics
        "usage": usage,
        "cost": cost,
        "time": time,
        "timestamp": datetime.now().isoformat(),
        "source": source,

        # Structured Data (JSON)
        "structured_summary": structured_data,

        # Promoted columns (also extracted from structured_summary by save_analysis)
        "target": structured_data.get("target"),
        "target_gene_names_list": structured_data.get("target_gene_names"),
        "selectivity_grade": structured_data.get("selectivity_grade"),
        "mechanism": structured_data.get("mechanism"),

        # Critic / Audit
        "citation_score": getattr(context, "citation_score", None),
        "citation_stats": getattr(context, "citation_stats", {})
    }
