import time
import logging
from core.context import Context
from core.llm import generate_with_search
from core.prompt_factory import PromptFactory

logger = logging.getLogger(__name__)


def generate_reviewer_report(name: str, temp: float, top_p: float, role: str, *,
                             drug_name: str, context: Context, pi_report: str,
                             compound_identity: str, specialist_reports: dict):
    """
    Runs one reviewer on the PI's draft report.
    Returns (name, review text). After 3 failed attempts the text is an error
    message, so one reviewer failing does not stop the others.

    `role` picks the prompt (neutral, critic or selectivity); `temp` and `top_p`
    override the generation config for this reviewer.
    """
    logger.info(f"Starting reviewer {name}", extra={"drug": drug_name})

    # Map reviewer name to stage key (distinct usage/cost bucket per reviewer)
    stage_key = {
        "Reviewer #1": "reviewer_1",
        "Reviewer #2": "reviewer_2",
        "Selectivity Reviewer": "reviewer_selectivity",
    }.get(name, "reviewer_2")

    prompt = PromptFactory.build_reviewer_prompt(
        drug_name=drug_name,
        context=context,
        pi_report=pi_report,
        reviewer_type=role,
        compound_identity=compound_identity,
        specialist_reports=specialist_reports,
    )

    response_text = ""
    last_error = ""

    for attempt in range(3):
        # Pass custom config
        resp = generate_with_search(
            prompt,
            generation_config_overrides={"temperature": temp, "top_p": top_p}
        )

        if resp.get("text") and not resp.get("error"):
            response_text = resp.get("text")
            # Log Usage
            context.add_usage(resp.get("usage"), stage=stage_key)
            break

        # NB: use `or`, not .get's default — an empty grounded response returns
        # {"text": None} (key present, value None), so a plain .get(...,'No text')
        # would yield None and crash on the [:100] slice.
        logger.warning(f"Reviewer {name} attempt {attempt+1}/3 failed: {(resp.get('text') or 'No text')[:100]}", extra={"drug": drug_name})
        last_error = resp.get("text") or "Unknown error"
        time.sleep(2)

    if not response_text:
        response_text = f"Error: No text returned from {name}. Details: {last_error}"
        logger.warning(f"{name} failed after retries.")

    return name, response_text
