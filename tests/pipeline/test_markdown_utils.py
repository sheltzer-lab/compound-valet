from core.markdown_utils import sanitize_markdown

def test_sanitization():
    test_cases = [
        # 1. Backticks around links
        ("Check this `[Paper 1](https://example.com)`.", "Check this [Paper 1](https://example.com)."),
        
        # 2. Space between brackets and parens
        ("Link: [Label] (https://example.com)", "Link: [Label](https://example.com)"),
        
        # 3. Spaces inside parens
        ("Link: [Label](  https://example.com  )", "Link: [Label](https://example.com)"),
        
        # 4. Malformed headers
        ("#Header 1\n##Header 2\n### Header 3", "# Header 1\n## Header 2\n### Header 3"),
        
        # 5. Unclosed code block
        ("Some code:\n```python\nprint('hello')", "Some code:\n```python\nprint('hello')\n```"),
        
        # 6. Combo!
        ("#Bad Header\nCheck out `[this link] ( https://link.com )` in this code:\n```json\n{\"id\": 1}", 
         "# Bad Header\nCheck out [this link](https://link.com) in this code:\n```json\n{\"id\": 1}\n```"),
         
        # 7. Multiple spaces
        ("Too many\n\n\n\nlines.", "Too many\n\nlines.")
    ]
    
    passed = 0
    for i, (input_text, expected) in enumerate(test_cases):
        output = sanitize_markdown(input_text)
        if output == expected:
            print(f"✅ Case {i+1} passed")
            passed += 1
        else:
            print(f"❌ Case {i+1} failed")
            print(f"   Input:    {repr(input_text)}")
            print(f"   Expected: {repr(expected)}")
            print(f"   Actual:   {repr(output)}")
            
    print(f"\nSummary: {passed}/{len(test_cases)} cases passed")

if __name__ == "__main__":
    test_sanitization()
