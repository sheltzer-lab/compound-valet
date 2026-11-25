import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.citation_manager import run_citation_manager

class TestCitationManager(unittest.TestCase):
    
    @patch('core.citation_manager.ReferenceChecker')
    def test_run_citation_manager_verified(self, mock_checker_cls):
        # Setup mock checker instance
        mock_checker = MagicMock()
        mock_checker_cls.return_value = mock_checker
        
        # Mock verify_references_batch to return success
        async def mock_batch(refs):
            return [{
                "reference": {"original_paper_obj": refs[0]["original_paper_obj"]}, 
                "verified": True, 
                "url": "https://doi.org/10.123/456",
                "source": "Semantic Scholar",
                "verified_match": {
                    "title": "Verified Title", 
                    "authors": ["Smith"],
                    "year": "2023"
                }
            }]
        
        mock_checker.verify_references_batch.side_effect = mock_batch
        
        papers = [{
            "title": "Test Paper",
            "url": "http://broken.com",
            "authors": "Smith",
            "year": "2023"
        }]
        
        # Run
        verified, report, score, stats, _, _ = run_citation_manager(papers, "DrugX")
        
        # Verify
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]['url'], "https://doi.org/10.123/456")
        self.assertTrue(verified[0]['link_valid'])
        self.assertEqual(stats['verified_count'], 1)

    @patch('core.citation_manager.ReferenceChecker')
    def test_run_citation_manager_failed(self, mock_checker_cls):
        # Setup mock checker instance
        mock_checker = MagicMock()
        mock_checker_cls.return_value = mock_checker
        
        # Mock verify_references_batch to return a failure
        async def mock_batch(refs):
            return [{
                "reference": {"original_paper_obj": refs[0]["original_paper_obj"]}, 
                "verified": False, 
                "error": "Not found"
            }]
            
        mock_checker.verify_references_batch.side_effect = mock_batch
        
        papers = [{
            "title": "Non-existent Paper",
            "url": "http://fake.com",
            "authors": "Doe",
            "year": "2023"
        }]
        
        # Run
        verified, report, score, stats, _, _ = run_citation_manager(papers, "DrugX")
        
        # Verify
        self.assertEqual(len(verified), 0)
        self.assertEqual(stats['excluded_count'], 1)
        self.assertEqual(stats['pass_rate'], 0)
        # excluded_papers should be present in stats with failure details
        self.assertIn('excluded_papers', stats)
        self.assertEqual(len(stats['excluded_papers']), 1)
        self.assertEqual(stats['excluded_papers'][0]['exclusion_reason'], 'Not found')

if __name__ == '__main__':
    unittest.main()
