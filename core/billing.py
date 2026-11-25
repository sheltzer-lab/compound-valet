from typing import Dict, Optional

# Pricing configurations (PER 1 MILLION TOKENS)
# Updated to tiered pricing based on context length
TIERS = {
    "standard": { # <= 200k tokens
        "input": 2.00,
        "output": 12.00
    },
    "high_context": { # > 200k tokens
        "input": 4.00,
        "output": 18.00
    }
}

# Google Search Grounding pricing
# $14 per 1,000 queries (after 5k free monthly quota)
# https://cloud.google.com/vertex-ai/generative-ai/pricing
GROUNDING_COST_PER_QUERY = 14.00 / 1000  # $0.014

def calculate_cost(usage: Dict[str, int], model_name: Optional[str] = None) -> float:
    """
    Calculates the estimated cost in USD based on usage and tiered pricing.

    Tiers:
      <= 200k prompt tokens: $2.00 / $12.00 per 1M
      > 200k prompt tokens:  $4.00 / $18.00 per 1M

    Thinking tokens (from reasoning models) are billed at the output rate.
    Google Search grounding queries are billed at $14 per 1,000 queries.

    Args:
        usage: Dict containing 'prompt_tokens', 'completion_tokens',
               and optionally 'thoughts_tokens' and 'search_grounding_queries'
        model_name: Ignored (standardized pricing applied)

    Returns:
        Float representing cost in USD.
    """
    if not usage:
        return 0.0

    p_tokens = usage.get("prompt_tokens", 0) or 0
    c_tokens = usage.get("completion_tokens", 0) or 0
    th_tokens = usage.get("thoughts_tokens", 0) or 0
    grounding_queries = usage.get("search_grounding_queries", 0) or 0

    # Determine Tier
    if p_tokens > 200_000:
        prices = TIERS["high_context"]
    else:
        prices = TIERS["standard"]

    cost_input = (p_tokens / 1_000_000) * prices["input"]
    cost_output = ((c_tokens + th_tokens) / 1_000_000) * prices["output"]
    cost_grounding = grounding_queries * GROUNDING_COST_PER_QUERY

    return round(cost_input + cost_output + cost_grounding, 4)
