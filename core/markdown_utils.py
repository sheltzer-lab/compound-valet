import re

def sanitize_markdown(text: str) -> str:
    """
    Programmatically fixes common markdown errors in LLM outputs.
    
    Includes:
    - Fixing spacing in links: [Label] (URL) -> [Label](URL)
    - Removing backticks around links: `[Label](URL)` -> [Label](URL)
    - Fixing space inside URL parens: [Label]( URL ) -> [Label](URL)
    - Fixing malformed headers: #Header -> # Header
    - Ensuring unclosed code blocks are closed
    - Cleaning up trailing white space
    """
    if not text:
        return ""
        
    # 1. Fix space between bracket and paren: [Label] (url) -> [Label](url)
    text = re.sub(r'(\[[^\]]+\])\s+(\([^\)]+\))', r'\1\2', text)
    
    # 2. Fix space inside parentheses: [Label](  url  ) -> [Label](url)
    # We only do this if it looks like a URL inside to avoid breaking complex MD
    text = re.sub(r'(\[[^\]]+\])\(\s*(https?://[^\s\)]+)\s*\)', r'\1(\2)', text)

    # 3. Remove backticks around links: `[Label](url)` -> [Label](url)
    text = re.sub(r'`(\[[^\]]+\]\([^\)]+\))`', r'\1', text)
    
    # 4. Fix malformed headers (missing space): #Header -> # Header
    # Matches 1-6 # at start of line followed by non-space non-#
    text = re.sub(r'^(#{1,6})([^# \n\t])', r'\1 \2', text, flags=re.MULTILINE)
    
    # 5. Fix unclosed code blocks
    # Count occurrences of ```
    code_block_count = len(re.findall(r'```', text))
    if code_block_count % 2 != 0:
        # Odd number means one is unclosed
        text = text.rstrip() + "\n```"
        
    # 6. Normalize trailing empty lines (max 2)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()
