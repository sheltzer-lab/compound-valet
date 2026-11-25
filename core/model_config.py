import os
from dotenv import load_dotenv

# Load env vars early
load_dotenv()

"""
Canonical configuration for Gemini models across all execution paths.

This module serves as the single source of truth for model parameters to prevent configuration drift
and ensure consistent quality regardless of the execution method.
"""

from google.genai import types

# Vertex AI Configuration
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
# Cloud Storage bucket for Gemini batch jobs on Vertex AI (Paper Query). Only batch jobs use it.
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")

# Automatically enable Vertex AI mode if Project ID is set
USE_VERTEX_AI = bool(PROJECT_ID)


# Default Generation Config
# Optimized for factual extraction and verification tasks.
# Temperature 0.1 is critical to prevent hallucinations and infinite loops in Batch API.
# No max_output_tokens: the model's thinking counts against it, and a cap cuts the answer off.
GENERATION_CONFIG = {
    "temperature": 0.05,
    "top_p": 0.1,
    "candidate_count": 1
}

# Default Safety Settings
# Permissive settings to avoid false positives on medical/biological content.
SAFETY_SETTINGS = [
    types.SafetySetting(
        category="HARM_CATEGORY_HATE_SPEECH",
        threshold="BLOCK_ONLY_HIGH",
    ),
    types.SafetySetting(
        category="HARM_CATEGORY_DANGEROUS_CONTENT",
        threshold="BLOCK_ONLY_HIGH",
    ),
    types.SafetySetting(
        category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
        threshold="BLOCK_ONLY_HIGH",
    ),
    types.SafetySetting(
        category="HARM_CATEGORY_HARASSMENT",
        threshold="BLOCK_ONLY_HIGH",
    ),
]
