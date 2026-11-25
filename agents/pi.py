import time
import re
import json
import logging
from core.context import Context
from core.llm import generate_with_search
from core.prompt_factory import PromptFactory
from core.markdown_utils import sanitize_markdown


from core.utils import extract_json_from_llm_response, sanitize_markdown_field

logger = logging.getLogger(__name__)

def process_pi_response(text: str) -> tuple:
    """
    Processes the raw text from the PI agent to extract structured data 
    and clean the report text.
    
    Reads the JSON block first, then falls back to the Markdown fields when the
    model leaves the JSON out.
    """
    full_text = text
    structured_data = {}

    # 1. Try Strict / Robust JSON extraction first
    json_data = extract_json_from_llm_response(full_text)
    
    # Handle wrapped responses (e.g. {"text": "..."})
    if json_data and isinstance(json_data, dict):
        for key in ["pi_report", "report", "text", "content"]:
            if key in json_data and isinstance(json_data[key], str):
                full_text = json_data[key]
                # Re-run extraction on inner text if needed
                inner_json = extract_json_from_llm_response(full_text)
                if inner_json: 
                    structured_data = inner_json
                break
        else:
            # If no wrapper key found, assume json_data IS the structured data
            # But only if it has relevant keys
            if "final_verdict" in json_data or "target" in json_data:
                structured_data = json_data

    # 2. Try strict regex block if utils failed or returned wrapper without data
    if not structured_data:
        match = re.search(r'```json\n(.*?)\n```', full_text, re.DOTALL)
        if match:
            json_str = match.group(1)
            try:
                structured_data = json.loads(json_str)
                # Remove the block from the report text for cleaner display
                clean_text = full_text.replace(match.group(0), "").strip()
                print(f"   ✅ Parsed structured data from PI report (Strict)")
            except Exception as e:
                print(f"   ⚠️ Failed to parse JSON from PI report: {e}")
                clean_text = full_text
        else:
            clean_text = full_text
    else:
        # If utils found it, try to clean it from text if it was a block
        clean_text = full_text
        # Naive cleanup of JSON block if present
        match = re.search(r'```json\s*\n?.*?\n?\s*```', full_text, re.DOTALL | re.IGNORECASE)
        if match:
             clean_text = full_text.replace(match.group(0), "").strip()


    # 3. Fallback: Parse Markdown fields if JSON failed
    # The model sometimes leaves out the JSON block.
    if not structured_data or (not structured_data.get('final_verdict') and not structured_data.get('target')):
        print(f"   ⚠️ No JSON block found, attempting Markdown fallback")
        
        # Verdict
        verdict_match = re.search(r'\*\*Verdict\*\*:\s*(.*?)(?:\n|$)', full_text, re.IGNORECASE)
        if verdict_match:
            structured_data['final_verdict'] = sanitize_markdown_field(verdict_match.group(1).strip())
            
        # Target
        target_match = re.search(r'\*\*Target.*?\*\*:\s*(.*?)(?:\n|$)', full_text, re.IGNORECASE)
        if target_match:
            structured_data['target'] = sanitize_markdown_field(target_match.group(1).strip())
            
        # Selectivity
        selectivity_match = re.search(r'\*\*Selectivity\*\*:\s*(.*?)(?:\n|$)', full_text, re.IGNORECASE)
        if selectivity_match:
            raw_selectivity = sanitize_markdown_field(selectivity_match.group(1).strip())
            grade_match = re.match(r'^([A-Z\s]+)', raw_selectivity)
            if grade_match:
                structured_data['selectivity_grade'] = grade_match.group(1).strip()
            else:
                structured_data['selectivity_grade'] = raw_selectivity

        if structured_data:
            print(f"   ✅ Parsed structured data from Markdown")

    clean_text = _strip_mechanism_class_line(clean_text)
    clean_text = sanitize_markdown(clean_text)

    return clean_text, structured_data


# Mechanism class is a STRUCTURED-only field (the JSON `mechanism` -> promoted column); it must
# never render in the report prose. The persona says so, but models/reviewers sometimes still write
# a "Mechanism Class: ..." line into the markdown — strip it here so it can never appear.
#
# The words must BE the label, not merely start it: they may be followed by a separator (the usual
# "Mechanism Class: Inhibitor") or by nothing at all (a bare "## Mechanism Class" heading), and
# nothing else. Without that, a line opening "Mechanism Class Rationale: ..." matched and the whole
# sentence was deleted — reviewers write lines like that routinely ("Mechanism Class Adherence",
# "Mechanism Class Rule Violation"), so it was only luck that the strip runs on the PI report alone.
# Mid-sentence mentions and the "Mechanism of Action" narrative were never matched and still aren't.
_MECHANISM_CLASS_LINE = re.compile(
    r'(?im)^[ \t>*\-]*#{0,6}[ \t]*\*{0,2}mechanism[ \t]+class\*{0,2}[ \t]*(?:[:—-][^\n]*)?\n?$')


def _strip_mechanism_class_line(text: str) -> str:
    if not text:
        return text
    return _MECHANISM_CLASS_LINE.sub("", text)

def generate_pi_report(context: Context, genetics_report: str, biochem_report: str, biophysics_report: str) -> tuple:
    """
    Synthesizes all specialist reports into a final PI decision.
    
    Uses PromptFactory to build the prompt.
    
    Returns:
        tuple: (report_text, structured_data)
    """
    print(f"\n🎯 Generating PI Report for {context.drug_name}...")
    
    # Use PromptFactory to build the prompt
    reports = {
        "genetics": genetics_report,
        "biochemistry": biochem_report,
        "biophysics": biophysics_report
    }
    
    try:
        prompt = PromptFactory.build_pi_prompt(context.drug_name, context, reports)
    except FileNotFoundError:
        print("   ❌ PI prompt file not found")
        return "Error: PI prompt file not found.", {}

    # Call LLM with search to allow double-checking citations if needed
    print(f"   🤖 Calling LLM for PI synthesis...")
    start_time = time.time()
    
    try:
        response = generate_with_search(prompt)
        duration = time.time() - start_time
        context.add_time(duration)
        
        print(f"   ✅ LLM response received ({duration:.1f}s)")
    except Exception as e:
        print(f"   ❌ LLM call failed: {e}")
        return f"Error: Failed to generate PI report - {e}", {}

    # An empty answer ({"text": None}) is a failed call, like an API error.
    if response.get("text") and not response.get("error"):
        context.add_usage(response.get("usage"), stage="pi_initial")
        # Delegate processing to shared function
        clean_text, structured_data = process_pi_response(response["text"])
        
        print(f"   ✅ PI report generated ({len(clean_text)} chars)")
        return clean_text, structured_data
    else:
        print("   ❌ No text in LLM response")
        return "Error: Failed to generate PI report.", {}


def revise_pi_report(context: Context, genetics_report: str, biochem_report: str, biophysics_report: str, *,
                     drug_name: str, reviewer_reports: dict, missing_papers_section: str, draft_report: str):
    """
    Revises the PI's draft report after the reviewers have read it.

    The prompt carries the specialist reports, the reviewers' reports, the papers added
    from the reviewers' suggestions (`missing_papers_section`) and the draft.

    Returns:
        tuple: (report_text, structured_data), or None if all 3 attempts failed;
        the caller then keeps the draft.
    """
    reports = {
        "genetics": genetics_report,
        "biochemistry": biochem_report,
        "biophysics": biophysics_report
    }

    pi_revision_prompt = PromptFactory.build_pi_prompt(
        drug_name=drug_name,
        context=context,
        specialist_reports=reports,
        feedback=reviewer_reports,
        missing_papers_section=missing_papers_section,
        initial_report=draft_report
    )

    # Call LLM for PI Revision with Retry
    pi_rev_start = time.time()
    pi_rev_response = {}
    for attempt in range(3):
        pi_rev_response = generate_with_search(pi_revision_prompt)
        if pi_rev_response.get("text") and not pi_rev_response.get("error"):
            break
        # NB: use `or`, not .get's default — an empty grounded response returns
        # {"text": None} (key present, value None), and None[:100] would crash.
        logger.warning(f"PI revision attempt {attempt+1}/3 failed: {(pi_rev_response.get('text') or 'No text')[:100]}", extra={"drug": drug_name})
        time.sleep(2)

    # Log PI Revision Usage
    context.add_usage(pi_rev_response.get("usage"), stage="pi_revision")
    context.add_time(time.time() - pi_rev_start)

    if pi_rev_response.get("text") and not pi_rev_response.get("error"):
        # Process the REVISED report
        revised = process_pi_response(pi_rev_response["text"])
        logger.info("PI revision complete", extra={"drug": drug_name})
        return revised
    logger.warning("PI revision failed, using draft", extra={"drug": drug_name})
    return None
