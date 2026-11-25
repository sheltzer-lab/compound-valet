import os
import re
import time
import logging
import threading
from google import genai
from google.genai import types
from dotenv import load_dotenv

from .model_config import GENERATION_CONFIG, SAFETY_SETTINGS
from .client_factory import get_client

# Get a logger for this module
logger = logging.getLogger(__name__)

# Suppress INFO logs from google-genai SDK
logging.getLogger("google_genai").setLevel(logging.WARNING)

# Load environment variables
load_dotenv()

# Configuration
MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-3.1-pro-preview")
MAX_503_RETRIES = 2
MAX_429_RETRIES = 5  # More retries for 429s in Flex PayGo mode

# Global pause state for 503 errors (thread-safe)
_pause_lock = threading.Lock()
_pause_until = 0  # Unix timestamp when pause expires
_pause_started = 0  # When the first pause in this sequence began
_PAUSE_DURATION_SECONDS = 180  # 3 minutes
_MAX_TOTAL_PAUSE_SECONDS = 600  # 10 minutes max cumulative pause


def _check_and_wait_for_pause():
    """Check if we're in a global pause state and wait if needed."""
    global _pause_until

    with _pause_lock:
        if time.time() < _pause_until:
            wait_time = _pause_until - time.time()
            logger.warning(f"Global pause active: waiting {wait_time:.0f}s for Gemini service recovery")

    # Wait outside the lock so other threads can also check
    while time.time() < _pause_until:
        remaining = _pause_until - time.time()
        if remaining > 0:
            time.sleep(min(10, remaining))


def _trigger_global_pause():
    """Trigger a global pause for all Gemini queries. Capped at _MAX_TOTAL_PAUSE_SECONDS total."""
    global _pause_until, _pause_started

    with _pause_lock:
        now = time.time()

        # If no active pause, start tracking
        if now >= _pause_until:
            _pause_started = now

        # Cap: don't extend beyond max total pause from first trigger
        max_allowed = _pause_started + _MAX_TOTAL_PAUSE_SECONDS
        new_pause_until = min(now + _PAUSE_DURATION_SECONDS, max_allowed)

        if new_pause_until <= now:
            logger.warning(f"503 global pause cap reached ({_MAX_TOTAL_PAUSE_SECONDS}s total) — not pausing further")
            return

        if new_pause_until > _pause_until:
            _pause_until = new_pause_until
            logger.warning(f"503 Service Unavailable - pausing until {new_pause_until - now:.0f}s from now")


def _extract_status_code(e, error_str: str):
    """Best-effort HTTP status for an SDK exception.

    Structured attributes first. Only if none is present do we look at the message,
    and then only where a status code actually appears — the google-genai errors read
    like "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, ...}}".

    This replaces five `or '503' in error_str` style tests. A bare substring search
    matches the digits anywhere in the payload — inside a quota limit, a retry delay in
    nanos, a trace id — so a 429 or 504 could be reclassified as a 503 and trigger a
    three-minute pause across every worker. Returns None when nothing is identifiable,
    and the caller logs the raw message in that case.
    """
    for attr in ("response", None):
        obj = getattr(e, attr, None) if attr else e
        for name in ("status_code", "code"):
            v = getattr(obj, name, None) if obj is not None else None
            if isinstance(v, int) and 100 <= v <= 599:
                return v
    m = re.match(r"\s*(\d{3})\b", error_str)          # leading "429 RESOURCE_EXHAUSTED..."
    if m and 100 <= int(m.group(1)) <= 599:
        return int(m.group(1))
    m = re.search(r"['\"]code['\"]\s*:\s*(\d{3})\b", error_str)   # {'code': 429}
    if m and 100 <= int(m.group(1)) <= 599:
        return int(m.group(1))
    return None


def generate_with_search(prompt: str, tools: list = None, model_name: str = None, _retry_count: int = 0, generation_config_overrides: dict = None, use_search: bool = True) -> dict:
    """
    Generates content using Gemini with Google Search tool enabled.
    
    Args:
        prompt: The prompt to send to the model
        tools: Optional list of tools (currently unused, google_search is always enabled)
        model_name: Optional model name override (defaults to MODEL_NAME from env)
        _retry_count: Internal retry counter (do not set manually)
        generation_config_overrides: Optional dict to override default generation config (e.g. temperature)
    """
    MAX_503_RETRIES = 2
    
    # Wait out any global pause before making the request
    _check_and_wait_for_pause()

    try:
        # Create client
        client = get_client()
        
        # Use provided model name or fall back to default
        target_model = model_name if model_name else MODEL_NAME

        # Prepare config
        gen_config = {
            "temperature": GENERATION_CONFIG['temperature'],
            "top_p": GENERATION_CONFIG['top_p'],
            "candidate_count": GENERATION_CONFIG['candidate_count'],
        }
        
        if generation_config_overrides:
            gen_config.update(generation_config_overrides)

        response = client.models.generate_content(
            model=target_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())] if use_search else None,
                temperature=gen_config['temperature'],
                max_output_tokens=gen_config.get('max_output_tokens'),   # None: no cap
                top_p=gen_config['top_p'],
                candidate_count=gen_config['candidate_count'],
                safety_settings=SAFETY_SETTINGS,
                response_mime_type="text/plain" 
            )
        )
        
        # Extract grounding metadata and count actual search queries
        grounding_meta = response.candidates[0].grounding_metadata if response.candidates else None
        search_queries = []
        if grounding_meta and grounding_meta.web_search_queries:
            search_queries = grounding_meta.web_search_queries

        if search_queries:
            logger.debug(f"Google Search Grounding: {len(search_queries)} queries")

        usage = {}
        if response.usage_metadata:
            usage = {
                "prompt_tokens": response.usage_metadata.prompt_token_count or 0,
                "completion_tokens": response.usage_metadata.candidates_token_count or 0,
                "total_tokens": response.usage_metadata.total_token_count or 0,
                "cached_tokens": getattr(response.usage_metadata, "cached_content_token_count", 0) or 0,
                "thoughts_tokens": getattr(response.usage_metadata, "thoughts_token_count", 0) or 0,
                "search_grounding_queries": len(search_queries),
            }

        result = {
            "text": response.text,
            "usage": usage,
            "grounding_metadata": grounding_meta,
            "search_queries": search_queries,
        }

        return result
        
    except Exception as e:
        error_str = str(e)
        status_code = _extract_status_code(e, error_str)
        if status_code is None:
            # Nothing identifiable — record the message so the next one is diagnosable
            # instead of silently landing in whichever branch its digits happen to match.
            logger.warning("Gemini error with no identifiable status code: %s", error_str[:200])
        
        if status_code == 503:
            # Log the originating error, not just the pause. A pause halts every worker
            # for 180s, and previously only its own message was recorded -- establishing
            # what these errors actually were took a Cloud Logging query against the
            # server-side audit log.
            logger.warning("503 from %s: %s", model_name or MODEL_NAME, error_str[:200])
            _trigger_global_pause()
            
            # Retry after the pause if we haven't exceeded retry limit
            if _retry_count < MAX_503_RETRIES:
                logger.warning(f"Retrying after 503 pause (attempt {_retry_count + 1}/{MAX_503_RETRIES})")
                return generate_with_search(prompt, tools, model_name, _retry_count + 1, generation_config_overrides, use_search)
            else:
                logger.error(f"Max 503 retries ({MAX_503_RETRIES}) exceeded")
        
        # Handle 499 (Client Closed/Cancelled) - Treat as timeout
        if status_code == 499:
            if _retry_count < MAX_503_RETRIES:
                logger.warning(f"499 Client Cancelled/Timeout - retrying (attempt {_retry_count + 1}/{MAX_503_RETRIES})")
                time.sleep(5)
                return generate_with_search(prompt, tools, model_name, _retry_count + 1, generation_config_overrides, use_search)
            else:
                logger.error("Max 499 retries exceeded")
        
        if status_code == 504:
            if _retry_count < MAX_503_RETRIES:
                wait_time = 30 * (_retry_count + 1)  # 30s, 60s backoff
                logger.warning(f"504 Gateway Timeout - waiting {wait_time}s before retry (attempt {_retry_count + 1}/{MAX_503_RETRIES})")
                time.sleep(wait_time)
                return generate_with_search(prompt, tools, model_name, _retry_count + 1, generation_config_overrides, use_search)
            else:
                logger.error(f"Max 504 retries ({MAX_503_RETRIES}) exceeded")
        
        # 502 Bad Gateway: transient upstream/gateway error (not a bad request); retry like 504.
        if status_code == 502:
            if _retry_count < MAX_503_RETRIES:
                wait_time = 10 * (_retry_count + 1)  # 10s, 20s backoff
                logger.warning(f"502 Bad Gateway - waiting {wait_time}s before retry (attempt {_retry_count + 1}/{MAX_503_RETRIES})")
                time.sleep(wait_time)
                return generate_with_search(prompt, tools, model_name, _retry_count + 1, generation_config_overrides, use_search)
            else:
                logger.error(f"Max 502 retries ({MAX_503_RETRIES}) exceeded")

        if status_code == 429:
            if _retry_count < MAX_429_RETRIES:
                # Exponential backoff with jitter to prevent thundering herd
                import random
                base_wait = 20 * (2 ** _retry_count)
                jitter = random.uniform(0, 10)  # 0-10s random jitter
                wait_time = base_wait + jitter
                logger.warning(f"429 Rate Limit - waiting {wait_time:.1f}s before retry (attempt {_retry_count + 1}/{MAX_429_RETRIES})")
                time.sleep(wait_time)
                return generate_with_search(prompt, tools, model_name, _retry_count + 1, generation_config_overrides, use_search)
            else:
                logger.error(f"Max 429 retries ({MAX_429_RETRIES}) exceeded")
        
        error_msg = f"Error communicating with Gemini API: {error_str}"
        if status_code:
            error_msg = f"HTTP {status_code}: {error_msg}"
            logger.error(f"API error: HTTP {status_code}")
        else:
            logger.error(f"API error: {error_str[:100]}")

        return {"text": error_msg, "usage": {}, "http_status": status_code, "error": True}
