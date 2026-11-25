import pytest
from unittest.mock import MagicMock, patch
from agents.pi import generate_pi_report
from core.context import Context

@patch("core.prompt_builder.get_verified_papers", return_value=[])
def test_generate_pi_report_mocked(mock_get_papers, mock_gemini_client, sample_context):
    """
    Test that the PI agent runs and returns a valid tuple using the mocked client.
    """
    # Configure mock response text to simulate PI output
    # THe PI agent usually returns text that might contain JSON. 
    # Let's mock a simple response.
    mock_gemini_client.models.generate_content.return_value.text = "This is the PI report."
    
    genetics = "Genetics findings"
    biochem = "Biochem findings"
    biophysics = "Biophysics findings"
    
    text, structured = generate_pi_report(sample_context, genetics, biochem, biophysics)
    
    assert text == "This is the PI report."
    # Structured data might be None if no JSON block was found
    assert structured is None or isinstance(structured, dict)
    
    # Verify usage was accessed (implying the call happened)
    # Just checking we didn't crash is good enough for a basic integration test
