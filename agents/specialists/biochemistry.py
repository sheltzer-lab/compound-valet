import time
from core.context import Context
from core.llm import generate_with_search
from core.prompt_factory import PromptFactory


def generate_biochemistry_report(context: Context) -> str:
    """
    Analyzes the provided Context from a Biochemistry perspective.
    Returns a Markdown report.
    
    Uses PromptFactory to build the prompt.
    """
    print(f"   🧪 Generating Biochemistry report...")
    
    # Use PromptFactory to build the prompt
    try:
        prompt = PromptFactory.build_specialist_prompt("biochemistry", context)
    except FileNotFoundError:
        return "Error: Biochemistry prompt file not found."
    
    # Run Agent with search enabled to allow cross-checking PDB IDs or specific constants
    start_time = time.time()
    response = generate_with_search(prompt)
    duration = time.time() - start_time
    context.add_time(duration)
    
    # An empty answer ({"text": None}) is a failed call, like an API error.
    if response.get("text") and not response.get("error"):
        context.add_usage(response.get("usage"), stage="biochemistry_specialist")
        raw_text = response["text"]
        
        # Some replies come wrapped in JSON
        from core.utils import extract_json_from_llm_response
        json_data = extract_json_from_llm_response(raw_text)
        
        if json_data and isinstance(json_data, dict):
            # Try to find the report content in likely keys
            for key in ["biochemistry_report", "report", "text", "content"]:
                if key in json_data and isinstance(json_data[key], str):
                    return json_data[key]
        
        return raw_text
    else:
        return "Error: Failed to generate biochemistry report."
