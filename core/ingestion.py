import logging
import os
import json
import time
from typing import List, Dict, Tuple
from .llm import generate_with_search
from .context import Context
from .citation_manager import run_citation_manager
from .prompt_factory import PromptFactory
from .concurrency import ContextThreadPoolExecutor

from .utils import fuzzy_match, normalize_string, is_duplicate

logger = logging.getLogger(__name__)



def _fetch_page(prompt: str) -> dict:
    """Helper to run LLM and return parsed JSON."""
    response = generate_with_search(prompt)
    if "text" not in response or response.get("error"):
        return {"error": response.get("text", "No text returned"), "usage": response.get("usage", {}),
                "call_failed": True}

    raw_text = response["text"]
    try:
        start_idx = raw_text.find('{')
        end_idx = raw_text.rfind('}')
        if start_idx == -1 or end_idx == -1:
             return {"error": "No JSON found", "raw_text": raw_text, "usage": response.get("usage", {})}
        
        json_str = raw_text[start_idx:end_idx+1]
        data = json.loads(json_str)
        data["_usage"] = response.get("usage", {})
        data["_grounding"] = response.get("grounding_metadata")
        return data
    except Exception as e:
        return {"error": str(e), "raw_text": raw_text, "usage": response.get("usage", {})}





def build_initial_context(drug_name: str, job_id: str = None) -> Context:
    """
    Orchestrates the 3-stage context gathering pipeline.

    NEW PIPELINE:
    1. Simple Literature Search (3 angles, basic metadata only)
    2. Citation Manager (programmatic verification with 3-attempt retry)
    3. Save to Database
    4. Return Context with verified papers

    Args:
        drug_name: Name of the drug to analyze
        job_id: set when this run's report is not being saved yet, so the
            papers are scoped to the job rather than replacing the live
            ones (see core/db.py's JobScopedVerifiedPaper)

    Returns:
        Context with verified papers
    """
    
    start_time = time.time()
    total_usage = {}
    
    logger.info("Building context", extra={"drug": drug_name})

    # --- Stage 1: Simple Literature Search ---
    logger.info("Stage 1: Literature search", extra={"drug": drug_name})
    
    merged_papers = []
    
    # Run searches in parallel using PromptFactory for consistent prompt construction
    import concurrent.futures

    def _process_angle(angle_def):
        """Worker to process a single angle."""
        # Use PromptFactory to build the prompt
        angle_prompt = PromptFactory.build_literature_search_prompt(
            drug_name=drug_name,
            angle=angle_def
        )

        data = _fetch_page(angle_prompt)
        return angle_def, data

    # Use SEARCH_ANGLES from PromptFactory - THE source of truth
    failed_calls = []
    with ContextThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_process_angle, a) for a in PromptFactory.SEARCH_ANGLES]
        
        for future in concurrent.futures.as_completed(futures):
            angle, data = future.result()
            
            # [FIX]: Always extract usage regardless of success/failure
            # _fetch_page puts it in "_usage" on success, or "usage" on error
            usage = data.pop("_usage", data.get("usage", {}))
            
            for k in ["prompt_tokens", "completion_tokens", "total_tokens", "thoughts_tokens", "cached_tokens", "search_grounding_queries"]:
                total_usage[k] = total_usage.get(k, 0) + (usage.get(k) or 0)
            
            if data.get("call_failed"):
                failed_calls.append(data["error"])
            if "error" not in data:
                new_papers = data.get("papers", [])
                for p in new_papers:
                    if not is_duplicate(p, merged_papers):
                        p['source_angle'] = angle['name']
                        merged_papers.append(p)
    
    # Every search's Gemini call failed (an outage, an expired login): fail the run rather than
    # go on as if the literature were empty.
    if len(failed_calls) == len(PromptFactory.SEARCH_ANGLES):
        raise RuntimeError(f"All {len(failed_calls)} literature searches failed: {failed_calls[0][:200]}")

    # Record literature_search stage searches before citation manager adds more
    lit_search_count = total_usage.get("search_grounding_queries", 0)

    logger.info(f"Found {len(merged_papers)} unique papers across 3 angles", extra={"drug": drug_name})

    # --- Stage 2: Citation Manager ---
    logger.info("Stage 2: Citation manager", extra={"drug": drug_name})
    
    verified_papers, audit_log, score, stats, critic_usage, critic_duration = run_citation_manager(merged_papers, drug_name)
    
    for k in ["prompt_tokens", "completion_tokens", "total_tokens", "thoughts_tokens", "cached_tokens", "search_grounding_queries"]:
        total_usage[k] = total_usage.get(k, 0) + (critic_usage.get(k) or 0)
    
    # --- Save to Database ---
    # With a job_id these land in job_scoped_verified_papers, so a run whose
    # report is never saved cannot replace the papers the live report cites.
    from .db import save_papers_standalone, get_verified_papers
    save_papers_standalone(drug_name, verified_papers, job_id)
    papers_from_db = get_verified_papers(drug_name, job_id)

    logger.info(f"Saved {len(papers_from_db)} verified papers", extra={"drug": drug_name})
    
    # Build paper ID mapping
    paper_id_map = {}
    for i, paper in enumerate(papers_from_db, 1):
        paper_id = f"{drug_name}-Paper-{i}"
        paper_id_map[paper_id] = paper
    
    total_duration = time.time() - start_time
    
    logger.info(f"Context complete in {total_duration:.1f}s — {len(papers_from_db)} papers, {total_usage.get('total_tokens', 0)} tokens", extra={"drug": drug_name})
    
    # Seed per-stage search tracking from ingestion
    if lit_search_count > 0:
        total_usage["search_by_stage"] = {"literature_search": lit_search_count}

    return Context(
        drug_name=drug_name,
        target_protein="Unknown",  # Will be extracted from evidence if needed
        job_id=job_id,             # carries the paper scope through the whole run
        relevant_papers=papers_from_db,
        verified_paper_ids=paper_id_map,
        chemical_properties={},
        usage_metadata=total_usage,
        audit_log=audit_log,
        citation_score=score,
        citation_stats=stats,
        time_stats={"total_seconds": total_duration}
    )
