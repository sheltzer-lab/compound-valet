import pytest
from core.billing import calculate_cost
from core.usage import sum_usage, extract_usage
from unittest.mock import MagicMock

# --- Billing Tests ---

def test_cost_calculation_standard():
    # 100k prompt tokens (<= 200k) -> Standard Tier
    # Input: $2.00/1M, Output: $12.00/1M
    usage = {"prompt_tokens": 100_000, "completion_tokens": 100_000}

    # Cost = (0.1 * 2.00) + (0.1 * 12.00) = 0.20 + 1.20 = 1.40
    cost = calculate_cost(usage, "any-model")
    assert cost == 1.40

def test_cost_calculation_high_context():
    # 300k prompt tokens (> 200k) -> High Context Tier
    # Input: $4.00/1M, Output: $18.00/1M
    usage = {"prompt_tokens": 300_000, "completion_tokens": 100_000}

    # Cost = (0.3 * 4.00) + (0.1 * 18.00) = 1.20 + 1.80 = 3.00
    cost = calculate_cost(usage, "any-model")
    assert cost == 3.00

def test_cost_calculation_with_thinking_tokens():
    # Thinking tokens should be billed at the output rate
    # Standard tier: Input $2.00/1M, Output $12.00/1M
    usage = {"prompt_tokens": 100_000, "completion_tokens": 50_000, "thoughts_tokens": 50_000}

    # Cost = (0.1 * 2.00) + ((0.05 + 0.05) * 12.00) = 0.20 + 1.20 = 1.40
    cost = calculate_cost(usage, "any-model")
    assert cost == 1.40

def test_cost_calculation_thinking_only():
    # Edge case: thinking tokens present but no completion tokens
    usage = {"prompt_tokens": 100_000, "completion_tokens": 0, "thoughts_tokens": 100_000}

    # Cost = (0.1 * 2.00) + (0.1 * 12.00) = 0.20 + 1.20 = 1.40
    assert calculate_cost(usage) == 1.40

def test_cost_calculation_no_thinking_key():
    # Backwards compatibility: no thoughts_tokens key at all
    usage = {"prompt_tokens": 100_000, "completion_tokens": 100_000}

    # Cost = (0.1 * 2.00) + (0.1 * 12.00) = 0.20 + 1.20 = 1.40
    assert calculate_cost(usage) == 1.40

def test_cost_calculation_boundary():
    # 200k prompt tokens (<= 200k) -> Standard Tier
    usage = {"prompt_tokens": 200_000, "completion_tokens": 0}
    # Cost = (0.2 * 2.00) = 0.40
    assert calculate_cost(usage) == 0.40

    # 200,001 prompt tokens (> 200k) -> High Context Tier
    usage = {"prompt_tokens": 200_001, "completion_tokens": 0}
    # Cost = (0.200001 * 4.00) = 0.800004 -> round to 4 decimals -> 0.8000
    assert calculate_cost(usage) == 0.8000

def test_cost_with_grounding():
    # Standard tier + 10 grounding queries
    # Token cost = (0.1 * 2.00) + (0.1 * 12.00) = 1.40
    # Grounding cost = 10 * 0.014 = 0.14
    # Total = 1.54
    usage = {
        "prompt_tokens": 100_000,
        "completion_tokens": 100_000,
        "search_grounding_queries": 10
    }
    assert calculate_cost(usage) == 1.54

def test_cost_grounding_only():
    # No tokens, just grounding queries
    usage = {"search_grounding_queries": 1000}
    # 1000 * $0.014 = $14.00
    assert calculate_cost(usage) == 14.0

def test_zero_usage():
    assert calculate_cost({}, "gemini-3.1-pro-preview") == 0.0

# --- Usage Tests ---

def test_sum_usage_dicts():
    current = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    new_data = {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55}

    result = sum_usage(current, new_data)
    assert result["prompt_tokens"] == 150
    assert result["completion_tokens"] == 15
    assert result["total_tokens"] == 165

def test_sum_usage_object():
    current = {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100}

    mock_obj = MagicMock()
    mock_obj.prompt_token_count = 10
    mock_obj.candidates_token_count = 10
    mock_obj.total_token_count = 20
    mock_obj.thoughts_token_count = 5

    result = sum_usage(current, mock_obj)
    assert result["prompt_tokens"] == 110
    assert result["completion_tokens"] == 10
    assert result["thoughts_tokens"] == 5

def test_sum_usage_with_thinking_dicts():
    current = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "thoughts_tokens": 50}
    new_data = {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55, "thoughts_tokens": 30}

    result = sum_usage(current, new_data)
    assert result["prompt_tokens"] == 150
    assert result["completion_tokens"] == 15
    assert result["thoughts_tokens"] == 80

def test_sum_usage_grounding_queries():
    current = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "search_grounding_queries": 3}
    new_data = {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55, "search_grounding_queries": 1}

    result = sum_usage(current, new_data)
    assert result["search_grounding_queries"] == 4

def test_sum_usage_grounding_missing_key():
    # Backwards compat: old usage dicts without grounding key
    current = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    new_data = {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55, "search_grounding_queries": 1}

    result = sum_usage(current, new_data)
    assert result["search_grounding_queries"] == 1

def test_extract_usage():
    raw_response = {
        "usageMetadata": {
            "promptTokenCount": 123,
            "candidatesTokenCount": 456,
            "totalTokenCount": 579,
            "thoughtsTokenCount": 200
        },
        "candidates": [{
            "groundingMetadata": {
                "webSearchQueries": [
                    "drug X mechanism of action",
                    "drug X clinical trials",
                    "drug X target protein binding",
                    "drug X phase 2 results",
                    "drug X FDA approval status"
                ]
            }
        }]
    }
    result = extract_usage(raw_response)
    assert result["prompt_tokens"] == 123
    assert result["completion_tokens"] == 456
    assert result["total_tokens"] == 579
    assert result["thoughts_tokens"] == 200
    assert result["search_grounding_queries"] == 5

def test_extract_usage_no_grounding():
    """extract_usage with no grounding metadata returns 0 queries."""
    raw_response = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
            "totalTokenCount": 150
        }
    }
    result = extract_usage(raw_response)
    assert result["search_grounding_queries"] == 0

def test_extract_usage_empty_queries():
    """extract_usage with empty webSearchQueries returns 0."""
    raw_response = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
            "totalTokenCount": 150
        },
        "candidates": [{
            "groundingMetadata": {
                "webSearchQueries": []
            }
        }]
    }
    result = extract_usage(raw_response)
    assert result["search_grounding_queries"] == 0
