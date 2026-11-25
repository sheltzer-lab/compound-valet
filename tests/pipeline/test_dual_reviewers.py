import concurrent.futures
import unittest
from unittest.mock import MagicMock, patch
import logging

# Ensure logging doesn't spam
logging.basicConfig(level=logging.ERROR)

from core.pipeline import run_analysis_pipeline
from core.context import Context

class TestDualReviewers(unittest.TestCase):
    
    @patch('core.pipeline.build_initial_context')
    @patch('core.pipeline.generate_genetics_report')
    @patch('core.pipeline.generate_biochemistry_report')
    @patch('core.pipeline.generate_biophysics_report')
    @patch('core.pipeline.generate_pi_report')
    @patch('agents.reviewers.generate_with_search')
    @patch('core.db.get_verified_papers') # Patch where it is defined/imported from
    @patch('core.pipeline.save_papers_standalone')
    @patch('core.pipeline.save_analysis')
    @patch('core.pipeline.get_analysis', return_value=None)
    def test_pipeline_runs_two_reviewers(self, mock_get_analysis, mock_save_analysis,
                                         mock_save_papers, mock_get_papers,
                                         mock_gen_search, mock_pi, mock_biophys,
                                         mock_biochem, mock_gen, mock_build_ctx):
        
        # Setup Context
        mock_ctx = MagicMock(spec=Context)
        mock_ctx.drug_name = "TestDrug"
        # Make them match so assertion passes
        mock_ctx.relevant_papers = [{"title": "p1", "url": "u1"}, {"title": "p2", "url": "u2"}] 
        mock_ctx.verified_paper_ids = {}
        mock_ctx.get_agent_context.return_value = "Context"
        mock_ctx.get_minimal_context.return_value = "MinContext"
        mock_ctx.summary.return_value = "Summary"
        mock_ctx.usage_metadata = {}
        
        mock_build_ctx.return_value = mock_ctx
        mock_get_papers.return_value = [{"title": "p1", "url": "u1"}, {"title": "p2", "url": "u2"}]
        
        # Setup Reports
        mock_gen.return_value = "Genetics Report"
        mock_biochem.return_value = "Biochem Report"
        mock_biophys.return_value = "Biophysics Report"
        mock_pi.return_value = ("Draft PI Report", {})
        
        # Setup Reviewer Responses
        # We need specific return values for different calls:
        # 1. Reviewer 1 (Creative)
        # 2. Reviewer 2 (Critic)
        # 3. PI Revision
        
        # Since calls happen potentially in parallel or sequence, side_effect iterator is tricky with threading
        # BUT run_analysis_pipeline uses ThreadPoolExecutor for specialists (which we mocked)
        # and then ThreadPoolExecutor for Reviewers (which use generate_with_search).
        # We can inspect the calls to generate_with_search to verify configs.
        
        def gen_search_side_effect(*args, **kwargs):
            prompt = args[0] if args else kwargs.get('prompt', '')
            overrides = kwargs.get('generation_config_overrides', {})
            
            if "Reviewer" in prompt or "Reviewer" in str(overrides): 
                # Helper to identify reviewer calls, though prompt might not strictly say "Reviewer" 
                # unless we check the content.
                # However, the overrides are the key signature!
                
                temp = overrides.get('temperature')
                if temp == 0.9:
                     return {"text": "Reviewer 1 Feedback", "usage": {}}
                elif temp == 0.05:
                     return {"text": "Reviewer 2 Feedback", "usage": {}}
                
            # Fallback for PI Revision (no overrides usually, or default)
            return {"text": "Final Revised Report", "usage": {}}

        mock_gen_search.side_effect = gen_search_side_effect
        
        # Run Pipeline
        # The PI revision lives in agents/pi.py and the reviewers in agents/reviewers.py; give them the same mocks.
        with patch('agents.pi.generate_with_search', mock_gen_search):
            result = run_analysis_pipeline("TestDrug")
        
        # Assertions
        # 1. Verify Result matches schema
        self.assertIn("reviewer_reports", result)
        reports = result["reviewer_reports"]
        self.assertIsInstance(reports, dict)
        self.assertIn("Reviewer #1", reports)
        self.assertIn("Reviewer #2", reports)
        self.assertEqual(reports["Reviewer #1"], "Reviewer 1 Feedback")
        self.assertEqual(reports["Reviewer #2"], "Reviewer 2 Feedback")
        
        # 2. Verify Configs
        # We expect at least 3 calls to generate_with_search (R1, R2, PI Rev)
        # Note: Retry logic might cause more if we failed, but we returned success.
        
        calls = mock_gen_search.call_args_list
        
        r1_called = False
        r2_called = False
        
        for call in calls:
            kwargs = call.kwargs
            overrides = kwargs.get('generation_config_overrides')
            if overrides:
                if overrides.get('temperature') == 0.9 and overrides.get('top_p') == 0.99:
                    r1_called = True
                if overrides.get('temperature') == 0.05 and overrides.get('top_p') == 0.1:
                    r2_called = True
                    
        self.assertTrue(r1_called, "Reviewer 1 with High Temp (0.9) was not called")
        self.assertTrue(r2_called, "Reviewer 2 with Low Temp was not called")

if __name__ == '__main__':
    unittest.main()
