"""
Shared target-matching logic, used by consensus and Paper Query.

Extracted from core/paper_query_engine.py to eliminate duplication across consumers.
"""

import json
import logging
import os
import re
import time
from typing import List

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def extract_pi_report_excerpt(pi_report: str) -> str:
    """Extract Verification Conclusion + Evidence Matrix sections from PI report."""
    if not pi_report:
        return ""

    # Find "Verification Conclusion" or "1. Verification Conclusion" section
    conclusion_match = re.search(
        r"(#+\s*\d*\.?\s*Verification Conclusion.*?)(?=\n#+\s*\d*\.?\s*(?:Evidence Matrix|Detailed)|\Z)",
        pi_report, re.DOTALL | re.IGNORECASE
    )
    # Find "Evidence Matrix" section
    matrix_match = re.search(
        r"(#+\s*\d*\.?\s*Evidence Matrix.*?)(?=\n#+\s*\d*\.?\s*(?:Detailed|$)|\Z)",
        pi_report, re.DOTALL | re.IGNORECASE
    )

    parts = []
    if conclusion_match:
        parts.append(conclusion_match.group(1).strip())
    if matrix_match:
        parts.append(matrix_match.group(1).strip())

    return "\n\n".join(parts) if parts else pi_report


# Two prompts share this call. Consensus (comparing two runs of one drug) uses the general
# one; Paper Query uses the paper one, which also accepts an adjacent step in the same pathway
# (ERK for a MEK inhibitor).
TARGET_MATCH_PROMPT = "target_match_prompt.md"
PAPER_TARGET_MATCH_PROMPT = "paper_target_match_prompt.md"

# The model that checks whether two targets are the same (consensus, Paper Query).
TARGET_MATCH_MODEL = os.getenv("GEMINI_TARGET_MATCH_MODEL", "gemini-3.8-flash")
_PROMPTS: dict = {}       # file name -> template, loaded once


def _get_target_match_prompt(name: str = TARGET_MATCH_PROMPT) -> str:
    """Load and cache a target match prompt template from prompts/."""
    if name not in _PROMPTS:
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", name)
        with open(prompt_path) as f:
            _PROMPTS[name] = f.read()
    return _PROMPTS[name]


def _extract_verdict_from_malformed_json(text: str) -> str | None:
    """Regex fallback to extract verdict from malformed JSON responses.

    Gemini sometimes returns truncated JSON where json.loads() fails but the
    verdict field is still present and readable — even without a closing quote.
    """
    if not text:
        return None
    # Try exact match first (with closing quote)
    m = re.search(r'"verdict"\s*:\s*"(verified|unverified)"', text, re.IGNORECASE)
    if m:
        return m.group(1).strip().lower()
    # Fallback: match without closing quote (truncated responses)
    m = re.search(r'"verdict"\s*:\s*"(unverified|verified)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip().lower()
    # Last resort: match partial verdict prefix
    m = re.search(r'"verdict"\s*:\s*"(unver|ver)', text, re.IGNORECASE)
    if m:
        return "unverified" if m.group(1).lower().startswith("unver") else "verified"
    return None


def llm_check_target_match(
    extracted_str: str, extracted_gene: str,
    db_target_str: str, db_target_list: List[str],
    drug_name: str, db_drug_name: str,
    db_selectivity: str, pi_report: str,
    prompt: str = TARGET_MATCH_PROMPT,
) -> bool | None:
    """
    Use Gemini 3.8 Flash to check whether a paper's target matches the DB target.

    Model chosen on a 465-pair bench: 0/210 false positives on negative controls, 0 failed
    calls, and faster than gemini-3-flash-preview, which had 3 unparseable responses and 1
    false positive on the same pairs.

    `prompt` is the template's file name in prompts/: TARGET_MATCH_PROMPT (consensus) or
    PAPER_TARGET_MATCH_PROMPT (Paper Query).

    Returns True (verified), False (unverified), or None (LLM call failed).
    Retries on 429/503/500 with exponential backoff.
    """
    from .client_factory import get_client
    from .model_config import SAFETY_SETTINGS

    try:
        client = get_client()
        prompt_template = _get_target_match_prompt(prompt)

        pi_excerpt = extract_pi_report_excerpt(pi_report)
        db_genes = ", ".join(db_target_list) if db_target_list else "None"
        paper_gene_str = extracted_gene if extracted_gene else "None"

        drug_block = (
            "--- Drug 1 ---\n"
            f"Paper Drug Name: {drug_name}\n"
            f"Paper Target: {extracted_str}\n"
            f"Paper Gene: {paper_gene_str}\n"
            f"DB Drug Name: {db_drug_name}\n"
            f"DB Target: {db_target_str}\n"
            f"DB Target Genes: {db_genes}\n"
            f"DB Selectivity: {db_selectivity}\n"
            f"PI Report Excerpt:\n{pi_excerpt}\n"
        )

        full_prompt = prompt_template + "\n\n## Drugs to Evaluate\n\n" + drug_block

        max_retries = 4
        last_response_text = None
        for attempt in range(max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=TARGET_MATCH_MODEL,
                    contents=full_prompt,
                    # No max_output_tokens: the model's thinking counts against it. At 1,024
                    # the thinking used ~980 tokens on hard cases and the answer was cut off
                    # (9 of 10 repeats of U0126 / "ERK", 2026-10-19), leaving the verdict to the
                    # regex fallback below.
                    config={
                        "response_mime_type": "application/json",
                        "temperature": 0.0,
                        "safety_settings": SAFETY_SETTINGS,
                    },
                )

                last_response_text = response.text
                parsed = json.loads(response.text)
                if isinstance(parsed, list):
                    parsed = parsed[0]

                verdict = parsed.get("verdict", "").strip().lower()
                return verdict == "verified"

            except json.JSONDecodeError:
                # Try regex fallback before retrying — Gemini often returns
                # truncated JSON where the verdict is still readable.
                fallback = _extract_verdict_from_malformed_json(last_response_text)
                if fallback is not None:
                    logger.info("LLM target match for %s: recovered verdict '%s' via regex fallback",
                                drug_name, fallback)
                    return fallback == "verified"

                if attempt < max_retries:
                    wait = 2 ** attempt + 1
                    logger.warning("LLM target match retry (attempt %d/%d) for %s, retrying in %ds: %s",
                                   attempt + 1, max_retries, drug_name, wait,
                                   str(last_response_text)[:120] if last_response_text else "empty response")
                    time.sleep(wait)
                    continue
                raise

            except Exception as err:
                err_str = str(err)
                is_retryable = ("429" in err_str or "503" in err_str
                                or "500" in err_str)
                if is_retryable and attempt < max_retries:
                    wait = 2 ** attempt + 1
                    logger.warning("LLM target match retry (attempt %d/%d) for %s, retrying in %ds: %s",
                                   attempt + 1, max_retries, drug_name, wait, err_str[:120])
                    time.sleep(wait)
                    continue
                raise

    except Exception as e:
        logger.error("LLM target match failed for %s: %s", drug_name, e)
        return None
