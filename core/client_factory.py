
import os
import logging
import google.auth.exceptions
from google import genai
from google.genai import errors

# Suppress INFO logs from google-genai SDK
logging.getLogger("google_genai").setLevel(logging.WARNING)
from core.model_config import PROJECT_ID, LOCATION, USE_VERTEX_AI

logger = logging.getLogger(__name__)

# The SDK does NOT retry unless this is set: with retry_options unset,
# google.genai._api_client.retry_args(None) returns stop_after_attempt(1), so every
# transient failure reaches the caller on its first attempt. Vertex returns UNAVAILABLE
# (gRPC 14 -> HTTP 503) on roughly 1% of calls under sustained load, and core/llm.py
# answers a 503 by pausing EVERY worker for 180s -- a fleet-wide stop for one failed
# request. Retrying the single request is the proportionate response; the pause then
# only fires when a request has already failed 5 times over ~15s, which is real evidence
# of a degraded backend.
#
# These are the SDK's own documented defaults, written out so the behaviour is visible
# here rather than implied by an absent argument.
RETRY_OPTIONS = {
    "attempts": 5,             # including the initial call
    "initial_delay": 1.0,      # ~1s, 2s, 4s, 8s with jitter
    "max_delay": 60.0,
    "http_status_codes": [408, 429, 500, 502, 503, 504],
}


def get_client(api_key: str = None):
    """
    Returns a configured Gemini Client.
    Uses Vertex AI if available (USE_VERTEX_AI=True), otherwise logic relies on API Key.
    """
    if USE_VERTEX_AI:
        # Vertex AI Initialization
        # Note: api_key is ignored in Vertex mode, credentials come from ADC
        client = genai.Client(
            vertexai=True,
            project=PROJECT_ID,
            location=LOCATION,
            http_options={"retry_options": RETRY_OPTIONS},
        )
        return client
    else:
        # AI Studio Initialization
        key = api_key or os.getenv("GOOGLE_API_KEY")
        if not key:
             raise ValueError("GOOGLE_API_KEY must be provided if Vertex AI is not configured.")
        return genai.Client(api_key=key)


class GeminiSetupError(Exception):
    """Gemini cannot be used with the current settings. The message says what to fix."""


LOGIN_COMMAND = "gcloud auth application-default login"


def check_gemini_setup(models) -> None:
    """Stop before any work starts if Gemini cannot be used with the current settings.

    Checks that the settings are present, then asks each model to count the tokens of a
    short text: free, and it needs the same access as a real call. Raises GeminiSetupError
    for a missing setting, a missing or expired login, a key that is not valid, refused
    access, or a model that is not available. Any other error (network, 429, 5xx) is only
    logged, so a passing problem never stops a run.
    """
    if not USE_VERTEX_AI and not os.getenv("GOOGLE_API_KEY"):
        raise GeminiSetupError(
            "Gemini access is not set up. In the .env file, set GOOGLE_CLOUD_PROJECT (Gemini on "
            "Vertex AI) or GOOGLE_API_KEY (Google AI Studio). See README: Settings.")
    where = (f"Vertex AI, project {PROJECT_ID}, location {LOCATION}" if USE_VERTEX_AI
             else "Google AI Studio, GOOGLE_API_KEY")
    try:
        client = get_client()
        for model in dict.fromkeys(models):
            try:
                client.models.count_tokens(model=model, contents="ok")
            except errors.ClientError as e:
                if e.code == 404:
                    hint = " Set GOOGLE_CLOUD_LOCATION=global." if USE_VERTEX_AI and LOCATION != "global" else ""
                    raise GeminiSetupError(
                        f"Gemini model {model} is not available ({where}).{hint} Google said: {e.message}")
                raise
    except GeminiSetupError:
        raise
    except google.auth.exceptions.DefaultCredentialsError:
        raise GeminiSetupError(f"No Google Cloud login was found for Vertex AI. Run: {LOGIN_COMMAND}")
    except google.auth.exceptions.RefreshError as e:
        raise GeminiSetupError(f"The Google Cloud login could not be used ({e}). Run: {LOGIN_COMMAND}")
    except errors.ClientError as e:
        if e.code == 400 and "API key" in str(e):
            raise GeminiSetupError(f"GOOGLE_API_KEY is not valid. Google said: {e.message}")
        if e.code in (401, 403):
            check = ("that the Vertex AI API is enabled in the project and that your account may use it"
                     if USE_VERTEX_AI else "that GOOGLE_API_KEY may use the Gemini API")
            raise GeminiSetupError(f"Gemini refused access ({where}). Check {check}. Google said: {e.message}")
        logger.warning("Could not check Gemini access (%s): %s", where, e)
    except Exception as e:
        logger.warning("Could not check Gemini access (%s): %s", where, e)
