"""
Configuration settings for Reference Checker
"""

import os
from typing import Dict, Any

# Default configuration
DEFAULT_CONFIG = {
    # API Settings
    "semantic_scholar": {
        "base_url": "https://api.semanticscholar.org/graph/v1",
        "rate_limit_delay": 1.0,
        "max_retries": 3,
        "timeout": 30,
        "api_key": None
    },
    
    # Processing Settings
    "processing": {
        "max_papers": 50,
        "batch_size": 100,
    },
    
    # Text Processing Settings
    "text_processing": {
        "similarity_threshold": 0.8,
        "max_title_similarity": 0.8,
        "max_author_similarity": 0.7,
        "year_tolerance": 1,
    }
}

def get_config() -> Dict[str, Any]:
    """Get configuration with environment variable overrides"""
    config = DEFAULT_CONFIG.copy()
    
    # Override with environment variables if present
    if os.getenv("SEMANTIC_SCHOLAR_API_KEY"):
        config["semantic_scholar"]["api_key"] = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    
    return config
