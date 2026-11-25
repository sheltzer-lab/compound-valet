import unittest
from unittest.mock import MagicMock, patch
import json
import sys
import os

# Add root to path
sys.path.append(os.getcwd())

from core.pipeline import run_analysis_pipeline
from core.context import Context

class TestEnsembleReviewer(unittest.TestCase):
    @patch("agents.reviewers.generate_with_search")
    @patch("core.pipeline.generate_genetics_report", return_value="Genetics Report")
    @patch("core.pipeline.generate_biochemistry_report", return_value="Biochem Report")
    @patch("core.pipeline.generate_biophysics_report", return_value="Biophys Report")
    @patch("core.pipeline.generate_pi_report", return_value=("Draft PI Report", {}))
    @patch("core.pipeline.resolve_paper_citations", side_effect=lambda x, y: x) # Pass through
    @patch("core.db.get_verified_papers", return_value=[])
    @patch("core.pipeline.build_initial_context") # Patch where it is used!
    @patch("core.pipeline.save_analysis")
    @patch("core.pipeline.get_analysis", return_value=None)
    def test_ensemble_logic(self, mock_get_analysis, mock_save_analysis, mock_ctx, mock_db, mock_resolve, mock_pi, mock_bp, mock_bc, mock_gen, mock_llm):

        # Setup Context
        ctx = Context(drug_name="TestDrug")
        ctx.relevant_papers = []
        ctx.verified_paper_ids = {}
        mock_ctx.return_value = ctx

        # Setup LLM Mock
        # Pipeline makes two calls to generate_with_search:
        # 1. Reviewer Call (first call)
        # 2. PI Revision Call (second call, prompt contains reviewer feedback)

        call_count = [0]  # Use list to allow mutation in closure

        def llm_side_effect(prompt, **kwargs):
            call_count[0] += 1
            
            # Helper to detect prompt type
            prompt_str = str(prompt)
            
            if "Role: critic" in prompt_str or "Reviewer #2" in prompt_str or "reviewer_type='critic'" in prompt_str:
                 print("DEBUG: Caught Reviewer #2 (Critic) Call")
                 return {"text": "Reviewer Feedback Content", "usage": {}}
            
            if "Role: neutral" in prompt_str or "Reviewer #1" in prompt_str:
                 print("DEBUG: Caught Reviewer #1 (Neutral) Call")
                 return {"text": "Neutral Feedback", "usage": {}}

            # If it's the PI revision, it should happen after reviewers
            # But since it's parallel, we rely on the prompt content check or order if synchronous (which it isn't completely)
            # However, the pipeline waits for reviewers before calling PI revision.
            # So PI revision is logically last. 
            # But let's just default to PI Revision if it looks like a PI prompt or if it's the 3rd call
            
            if "Revising the PI Report" in prompt_str or call_count[0] >= 3:
                print("DEBUG: Caught PI Revision Call")
                return {"text": "Final Revised Report", "usage": {}}

            return {"text": "Generic Response", "usage": {}}

        mock_llm.side_effect = llm_side_effect

        # Run Pipeline
        print("Running pipeline...")
        # The PI revision lives in agents/pi.py and the reviewers in agents/reviewers.py; give them the same mocks.
        with patch("agents.pi.generate_with_search", mock_llm):
            result = run_analysis_pipeline("TestDrug")

        # Verify Reviewer Report is a dictionary containing feedback and draft
        reviewer_reports = result['reviewer_reports']
        print(f"Reviewer Reports in Result: {reviewer_reports}")

        # Reviewer reports should contain the feedback text for Reviewer 2
        self.assertIn("Reviewer Feedback Content", reviewer_reports.get('Reviewer #2', ''))
        # The Selectivity Reviewer's result must be captured too (f3 -> reviewer_results)
        self.assertIn('Selectivity Reviewer', reviewer_reports)
        # Reviewer reports should also contain the appended draft
        self.assertIn("Draft PI Report", reviewer_reports.get('Original Draft', ''))

        # Verify generate_with_search was called 4 times
        # (Reviewer #1 + Reviewer #2 + Selectivity Reviewer + PI Revision)
        self.assertEqual(mock_llm.call_count, 4)

        # Verify PI Revision prompt contained reviewer feedback
        # It should be the last call
        pi_revision_call = mock_llm.call_args_list[-1]
        pi_prompt = pi_revision_call[0][0]
        self.assertIn("Reviewer Feedback Content", pi_prompt,
                      "PI revision prompt should contain reviewer feedback")

        print("SUCCESS: Reviewer logic verified.")

if __name__ == '__main__':
    unittest.main()
