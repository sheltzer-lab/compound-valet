import unittest
from unittest.mock import MagicMock, patch
import json
from core.context import Context
from core.pipeline import run_analysis_pipeline

class TestMissingPapersHandoff(unittest.TestCase):
    
    @patch('agents.reviewers.generate_with_search')
    @patch('core.citation_manager.run_citation_manager')
    @patch('core.db.save_papers_standalone')
    @patch('core.db.get_verified_papers')
    @patch('core.pipeline.build_initial_context')
    @patch('core.pipeline.generate_genetics_report')
    @patch('core.pipeline.generate_biochemistry_report')
    @patch('core.pipeline.generate_biophysics_report')
    @patch('core.pipeline.generate_pi_report')
    @patch('agents.pi.process_pi_response')
    @patch('core.pipeline.PromptFactory')
    def test_missing_papers_integration(self, 
                                        mock_prompt_factory,
                                        mock_process_pi,
                                        mock_pi_report,
                                        mock_biophys, mock_biochem, mock_genetics,
                                        mock_build_ctx,
                                        mock_get_verified,
                                        mock_save_papers,
                                        mock_citation_manager,
                                        mock_llm):
        
        # 1. Setup Context
        drug_name = "TestDrug"
        initial_papers = [{"title": "Old Paper", "year": "2020", "url": "http://old", "citation": "Old, 2020"}]
        ctx = Context(drug_name=drug_name, relevant_papers=initial_papers)
        # Manually set verified_paper_ids as pipeline expects it
        ctx.verified_paper_ids = {"TestDrug-Paper-1": initial_papers[0]}
        
        mock_build_ctx.return_value = ctx
        
        # 2. Setup DB Mock
        # We need a side_effect for get_verified_papers because it will be called multiple times
        # 1st call: inside run_analysis_pipeline (validation check)
        # 2nd call: inside missing papers block (merge)
        # 3rd call: inside missing papers block (refresh)
        # 4th call: prompt_builder.inject_verified_sources -> wait, prompt factory calls it.
        
        # Simulating DB state
        db_state = list(initial_papers)
        
        def get_papers_side_effect(name, job_id=None):
            return list(db_state)
            
        mock_get_verified.side_effect = get_papers_side_effect
        
        def save_papers_side_effect(name, papers, job_id=None):
            nonlocal db_state
            db_state = list(papers)
            
        mock_save_papers.side_effect = save_papers_side_effect
        
        # 3. Setup Reports
        mock_genetics.return_value = "Genetics Report"
        mock_biochem.return_value = "Biochem Report"
        mock_biophys.return_value = "Biophys Report"
        mock_pi_report.return_value = ("Draft PI Report", {})
        
        
        # 4. Setup Reviewer LLM Response
        missing_paper = {
            "title": "Critical Missing Paper", 
            "year": "2024", 
            "authors": ["Doe J"],
            "url": "http://new"
        }
        
        reviewer_json = json.dumps([missing_paper])
        # Use simple string without indentation issues
        reviewer_text = "Here is my critique.\n\n## Critical Missing Papers\n```json\n" + reviewer_json + "\n```"
        
        # Mock process_pi_response to return tuple
        mock_process_pi.return_value = ("Final Revised Report", {})
        
        # Three reviewers (Reviewer #1, Reviewer #2, Selectivity Reviewer) run in parallel
        # in nondeterministic order, then the PI Revision runs last. Give every reviewer the
        # JSON-bearing text (the missing-papers extraction dedups by title -> 1 candidate),
        # and the final call is the PI Revision.
        mock_llm.side_effect = [
            {"text": reviewer_text, "usage": {}},          # Reviewer (any of the 3)
            {"text": reviewer_text, "usage": {}},          # Reviewer (any of the 3)
            {"text": reviewer_text, "usage": {}},          # Reviewer (any of the 3)
            {"text": "Final Revised Report", "usage": {}}, # PI Revision (always last)
        ]
        
        # 5. Setup Citation Manager
        # Returns: verified_papers, audit_log, score, stats, usage, duration
        mock_citation_manager.return_value = ([missing_paper], "Audit Log", 100, {}, {}, 1.0)
        
        # 6. Run Pipeline
        # The PI revision lives in agents/pi.py and the reviewers in agents/reviewers.py; give them the same mocks.
        with patch('agents.pi.generate_with_search', mock_llm), \
             patch('agents.reviewers.PromptFactory', mock_prompt_factory), \
             patch('agents.pi.PromptFactory', mock_prompt_factory):
            run_analysis_pipeline(drug_name)
        
        # 7. Assertions
        
        # Check if Citation Manager was called
        mock_citation_manager.assert_called()
        args, _ = mock_citation_manager.call_args
        self.assertEqual(len(args[0]), 1)
        self.assertEqual(args[0][0]['title'], "Critical Missing Paper")
        
        # Check if DB was updated
        self.assertEqual(len(db_state), 2)
        self.assertEqual(db_state[1]['title'], "Critical Missing Paper")
        
        # CRITICAL: Check if PromptFactory.build_missing_papers_section was called
        self.assertTrue(mock_prompt_factory.build_missing_papers_section.called, "build_missing_papers_section not called")
        
        # Check arguments passed to build_pi_prompt
        self.assertTrue(mock_prompt_factory.build_pi_prompt.called)
        
        call_args = mock_prompt_factory.build_pi_prompt.call_args
        kwargs = call_args[1]
        
        self.assertIn('missing_papers_section', kwargs)
        # Note: missing_section value will be a MagicMock object because PromptFactory is a mock, 
        # unless we set return value for build_missing_papers_section.
        
        # If we want to check content, we should check what was passed TO build_missing_papers_section
        list_args = mock_prompt_factory.build_missing_papers_section.call_args[0][0]
        print(f"\nCaught Missing Papers List Text:\n{list_args}")
        # Updated to numbered list format
        self.assertIn("1. **Critical Missing Paper**", list_args)
        self.assertIn("TestDrug-Paper-2", list_args)
        
        # Check initial_report passed
        self.assertIn('initial_report', kwargs)
        self.assertEqual(kwargs['initial_report'], "Draft PI Report")
        
    @patch('agents.reviewers.generate_with_search')
    @patch('core.citation_manager.run_citation_manager')
    @patch('core.db.save_papers_standalone')
    @patch('core.db.get_verified_papers')
    @patch('core.pipeline.build_initial_context')
    @patch('core.pipeline.generate_genetics_report')
    @patch('core.pipeline.generate_biochemistry_report')
    @patch('core.pipeline.generate_biophysics_report')
    @patch('core.pipeline.generate_pi_report')
    @patch('agents.pi.process_pi_response')
    @patch('core.pipeline.PromptFactory')
    def test_failed_papers_annotated_in_reviewer_reports(self,
                                        mock_prompt_factory,
                                        mock_process_pi,
                                        mock_pi_report,
                                        mock_biophys, mock_biochem, mock_genetics,
                                        mock_build_ctx,
                                        mock_get_verified,
                                        mock_save_papers,
                                        mock_citation_manager,
                                        mock_llm):
        """When reviewer-suggested papers fail verification, admin notes should appear in final reviewer_reports."""

        # 1. Setup Context
        drug_name = "TestDrug"
        initial_papers = [{"title": "Old Paper", "year": "2020", "url": "http://old", "citation": "Old, 2020"}]
        ctx = Context(drug_name=drug_name, relevant_papers=initial_papers)
        ctx.verified_paper_ids = {"TestDrug-Paper-1": initial_papers[0]}
        mock_build_ctx.return_value = ctx

        # 2. Setup DB Mock
        db_state = list(initial_papers)
        mock_get_verified.side_effect = lambda name, job_id=None: list(db_state)
        mock_save_papers.side_effect = lambda name, papers, job_id=None: None

        # 3. Setup Reports
        mock_genetics.return_value = "Genetics Report"
        mock_biochem.return_value = "Biochem Report"
        mock_biophys.return_value = "Biophys Report"
        mock_pi_report.return_value = ("Draft PI Report", {})
        mock_process_pi.return_value = ("Final Revised Report", {})

        # 4. Setup Reviewer LLM Response — suggest two papers
        suggested_papers = [
            {"title": "Good Paper", "year": "2024", "authors": ["Smith A"], "url": "http://good"},
            {"title": "Bad Paper", "year": "2023", "authors": ["Doe J"], "url": "http://bad"}
        ]
        reviewer_json = json.dumps(suggested_papers)
        reviewer_text = "My critique.\n```json\n" + reviewer_json + "\n```"

        mock_llm.side_effect = [
            {"text": reviewer_text, "usage": {}},   # Reviewer #1
            {"text": "No issues found.", "usage": {}},  # Reviewer #2
            {"text": "Final Revised Report", "usage": {}}  # PI Revision
        ]

        # 5. Setup Citation Manager — one passes, one fails
        verified_paper = {"title": "Good Paper", "year": "2024", "authors": ["Smith A"], "url": "http://good", "citation": "Smith, 2024"}
        excluded_paper = {
            "title": "Bad Paper", "year": "2023", "authors": ["Doe J"], "url": "http://bad",
            "_source_reviewer": "Reviewer #1",
            "exclusion_reason": "No matching record found in Semantic Scholar or CrossRef"
        }

        cm_stats = {
            "total_citations": 2,
            "verified_count": 1,
            "excluded_count": 1,
            "pass_rate": 50.0,
            "excluded_papers": [excluded_paper]
        }
        mock_citation_manager.return_value = ([verified_paper], "Audit Log", 50, cm_stats, {}, 1.0)

        # 6. Run Pipeline
        # The PI revision lives in agents/pi.py and the reviewers in agents/reviewers.py; give them the same mocks.
        with patch('agents.pi.generate_with_search', mock_llm), \
             patch('agents.reviewers.PromptFactory', mock_prompt_factory), \
             patch('agents.pi.PromptFactory', mock_prompt_factory):
            result = run_analysis_pipeline(drug_name)

        # 7. Assertions — reviewer_reports should contain admin note
        reviewer_reports = result.get("reviewer_reports", {})
        r1_text = reviewer_reports.get("Reviewer #1", "")

        self.assertIn("[Admin Note] Reference Verification Failures", r1_text)
        self.assertIn("Bad Paper", r1_text)
        self.assertIn("No matching record found in Semantic Scholar or CrossRef", r1_text)

        # Reviewer #2 should NOT have an admin note (no failed papers from that reviewer)
        r2_text = reviewer_reports.get("Reviewer #2", "")
        self.assertNotIn("[Admin Note]", r2_text)

if __name__ == '__main__':
    unittest.main()
