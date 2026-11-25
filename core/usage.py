from typing import Dict, Any, Optional
from google.genai import types

def sum_usage(current: Dict[str, int], new_data: Any) -> Dict[str, int]:
    """
    Aggregates token usage from a new response object or dictionary into a running total.
    
    Args:
        current: Dictionary with 'prompt_tokens', 'completion_tokens', 'total_tokens'
        new_data: Can be a dict, a google.genai.types.UsageMetadata object, or None
        
    Returns:
        Updated dictionary with summed values.
    """
    if not current: 
        current = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not new_data: 
        return current
    
    p = c = t = 0
    
    # helper for attribute access
    def get_val(obj, key, attr):
        if isinstance(obj, dict):
            return obj.get(key, 0)
        return getattr(obj, attr, 0)

    # Handle different structures
    # 1. Dict from JSON (Batch API result)
    # 2. Dict from UsageMetadata (some SDK versions)
    # 3. Object from SDK
    
    sg = 0  # search grounding queries

    if isinstance(new_data, dict):
        # Check for standard keys vs Go/Proto keys
        if "prompt_tokens" in new_data:
            p = new_data.get("prompt_tokens") or 0
            c = new_data.get("completion_tokens") or 0
            t = new_data.get("total_tokens") or 0
            # Support cached tokens if present
            k = new_data.get("cached_tokens") or 0
            th = new_data.get("thoughts_tokens") or 0
            sg = new_data.get("search_grounding_queries") or 0
        else:
            # Fallback for raw JSON from Batch API (often has different casing or names)
            p = new_data.get("prompt_token_count") or 0
            c = new_data.get("candidates_token_count") or 0
            t = new_data.get("total_token_count") or 0
            # Try to find cached count in raw API response keys (snake or camel)
            k = new_data.get("cached_content_token_count") or new_data.get("cachedContentTokenCount") or 0
            th = new_data.get("thoughts_token_count") or new_data.get("thoughtsTokenCount") or 0
            sg = new_data.get("search_grounding_queries") or 0
    else:
        # Object access
        p = getattr(new_data, "prompt_token_count", 0) or 0
        c = getattr(new_data, "candidates_token_count", 0) or 0
        t = getattr(new_data, "total_token_count", 0) or 0
        k = getattr(new_data, "cached_content_token_count", 0) or 0
        th = getattr(new_data, "thoughts_token_count", 0) or 0
        sg = getattr(new_data, "search_grounding_queries", 0) or 0
        
    # Ensure ints
    if p is None: p = 0
    if c is None: c = 0
    if t is None: t = 0
    if k is None: k = 0
    if th is None: th = 0
    if sg is None: sg = 0

    # Sanitize current dict values in case they are None
    if current.get("prompt_tokens") is None: current["prompt_tokens"] = 0
    if current.get("completion_tokens") is None: current["completion_tokens"] = 0
    if current.get("total_tokens") is None: current["total_tokens"] = 0

    current["prompt_tokens"] += int(p)
    current["completion_tokens"] += int(c)
    current["total_tokens"] += int(t)
    current["cached_tokens"] = current.get("cached_tokens", 0) + int(k)
    current["thoughts_tokens"] = current.get("thoughts_tokens", 0) + int(th)
    current["search_grounding_queries"] = current.get("search_grounding_queries", 0) + int(sg)

    return current

def extract_usage(response_part: Dict[str, Any]) -> Dict[str, int]:
    """
    Extracts usage dictionary from a raw JSON response part (e.g. from Batch API).
    Standardizes it to the expected keys.
    Counts actual grounding queries from groundingMetadata.webSearchQueries.
    """
    raw = response_part.get("usageMetadata", {})

    # Count actual search grounding queries from the response
    grounding_query_count = 0
    candidates = response_part.get("candidates", [])
    if candidates:
        grounding_meta = candidates[0].get("groundingMetadata", {})
        web_queries = grounding_meta.get("webSearchQueries", [])
        grounding_query_count = len(web_queries) if web_queries else 0

    return {
        "prompt_tokens": raw.get("promptTokenCount", 0),
        "completion_tokens": raw.get("candidatesTokenCount", 0),
        "total_tokens": raw.get("totalTokenCount", 0),
        "cached_tokens": raw.get("cachedContentTokenCount", 0),
        "thoughts_tokens": raw.get("thoughtsTokenCount", 0),
        "search_grounding_queries": grounding_query_count,
    }
