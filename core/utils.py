import re
import difflib
from typing import List, Union

def normalize_string(s: str) -> str:
    """Lowercase, strip non-alphanumeric (except spaces), strip whitespace."""
    if not s: return ""
    # Keep only alphanumeric and spaces
    s = re.sub(r'[^a-z0-9\s]', '', s.lower())
    return " ".join(s.split())


def fuzzy_match(s1: str, s2: str, threshold: float = 0.9) -> bool:
    """
    Returns True if s1 and s2 are similar enough.
    """
    n1 = normalize_string(s1)
    n2 = normalize_string(s2)
    if not n1 or not n2:
        return False
    
    # Exact match shortcut
    if n1 == n2:
        return True
        
    return difflib.SequenceMatcher(None, n1, n2).ratio() > threshold

def is_duplicate(new_paper: dict, existing_papers: list) -> bool:
    """
    Checks if new_paper exists in existing_papers based on DOI, URL, or Fuzzy Title.
    """
    new_doi = new_paper.get("doi")
    new_url = new_paper.get("url")
    new_title = new_paper.get("title", "")
    
    for p in existing_papers:
        # 1. DOI Check
        if new_doi and p.get("doi") == new_doi:
            return True
            
        # 2. URL Check
        if new_url and p.get("url") == new_url:
            return True
            
        # 3. Fuzzy Title Check
        existing_title = p.get("title", "")
        if fuzzy_match(existing_title, new_title, threshold=0.9):
            # Title Matches. Now check Year discrepancy to avoid Protocol vs Paper issues.
            # If both have valid years and gap > 1, treating as different.
            
            y_old = str(p.get('year', '')).strip()
            y_new = str(new_paper.get('year', '')).strip()
            
            if y_old.isdigit() and y_new.isdigit():
                diff = abs(int(y_old) - int(y_new))
                if diff > 1:
                    # Likely different papers (e.g. 2018 vs 2021)
                    continue
            
            return True
            
    return False


def extract_json_from_llm_response(text: str) -> Union[dict, list, None]:
    """
    Robustly extract JSON from an LLM response.
    
    Handles common issues:
    - Markdown code blocks (```json ... ```)
    - Multiple JSON blocks (tries the largest one first)
    - Control characters in strings
    - Trailing content after JSON
    - Missing/malformed JSON
    
    Returns:
        Parsed JSON dict/list, or None if extraction fails
    """
    import json
    
    if not text or not text.strip():
        return None
    
    # Strategy 1: Look for markdown-fenced JSON blocks first (most reliable)
    json_block_pattern = r'```(?:json)?\s*\n([\s\S]*?)\n```'
    matches = re.findall(json_block_pattern, text)
    
    for match in matches:
        try:
            # Sanitize and parse
            sanitized = _sanitize_json_string(match)
            return json.loads(sanitized)
        except json.JSONDecodeError:
            continue
    
    # Strategy 2: Find all potential JSON objects (match balanced braces)
    candidates = _find_json_candidates(text)
    
    # Sort by length (longest first - likely to be the full response)
    candidates.sort(key=len, reverse=True)
    
    for candidate in candidates:
        try:
            sanitized = _sanitize_json_string(candidate)
            return json.loads(sanitized)
        except json.JSONDecodeError:
            continue
    
    return None


def _sanitize_json_string(text: str) -> str:
    """
    Sanitize a JSON string by fixing common issues.
    """
    # Remove control characters except standard whitespace (\n, \r, \t, space)
    # These are often the cause of "Invalid control character" errors
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)

    # Fix trailing commas before closing brackets (common LLM mistake)
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)

    return text.strip()


def _find_json_candidates(text: str) -> list:
    """
    Find potential JSON objects in text by matching balanced braces.
    Returns a list of candidate JSON strings.
    """
    candidates = []
    i = 0

    while i < len(text):
        if text[i] == '{':
            # Try to find matching closing brace
            depth = 0
            start = i
            in_string = False
            escape_next = False

            for j in range(i, len(text)):
                char = text[j]

                if escape_next:
                    escape_next = False
                    continue

                if char == '\\' and in_string:
                    escape_next = True
                    continue

                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue

                if not in_string:
                    if char == '{':
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0:
                            candidates.append(text[start:j+1])
                            i = j
                            break

        i += 1

    return candidates


def sanitize_markdown_field(text: str) -> str:
    """Remove markdown formatting like bolding, italics, etc."""
    if not text:
        return text
    # Remove bold/italic markers
    clean = re.sub(r'\*\*|__|\*|_', '', text)
    return clean.strip()


