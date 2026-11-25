import unittest
import os
import re
from core.citation_utils import resolve_paper_citations

class TestCriticalBehaviors(unittest.TestCase):
    """
    Tests to prevent regressions in critical system behaviors.
    """

    def test_citation_hydration_strips_backticks(self):
        """
        Regression Test: Ensure that citation keys wrapped in backticks (e.g., `[Drug-Paper-1]`)
        are correctly identified and hydrated, stripping the backticks.
        """
        # Scenario: Model outputs backticks around the citation key
        text = "This is verified `[Drug-Paper-1]`."
        
        paper_map = {
            "Drug-Paper-1": {
                "title": "Test Title",
                "authors": "T. Author",
                "year": "2024",
                "url": "https://example.com/paper1"
            }
        }
        
        # Action
        resolved_text = resolve_paper_citations(text, paper_map)
        
        # verification
        # Expectation: Backticks are gone, link is present
        # Note: formatting logic extracts last name, so "T. Author" becomes "Author"
        expected_fragment = "[Author, 2024](https://example.com/paper1)"
        self.assertIn(expected_fragment, resolved_text)
        self.assertNotIn("`[", resolved_text, "Backticks should be stripped")
        
        # Also test non-backticked works too
        text_normal = "This is verified [Drug-Paper-1]."
        resolved_normal = resolve_paper_citations(text_normal, paper_map)
        self.assertIn(expected_fragment, resolved_normal)


if __name__ == '__main__':
    unittest.main()
