"""Tests for Gemini error handling and error propagation."""

import time
import unittest
from unittest.mock import MagicMock, patch



class TestErrorResponseFlag(unittest.TestCase):
    """Verify generate_with_search marks error responses with 'error': True."""

    @patch("core.llm.get_client")
    def test_api_error_has_error_flag(self, mock_get_client):
        """When Gemini raises an exception, returned dict should have error=True."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("Something broke")
        mock_get_client.return_value = mock_client

        from core.llm import generate_with_search
        result = generate_with_search("test prompt")

        self.assertTrue(result.get("error"))
        self.assertIn("Something broke", result["text"])

    @patch("core.llm.get_client")
    def test_successful_response_has_no_error_flag(self, mock_get_client):
        """Successful responses should not have an error key."""
        mock_response = MagicMock()
        mock_response.text = "Valid response text"
        mock_response.candidates = []
        mock_response.usage_metadata = None
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_get_client.return_value = mock_client

        from core.llm import generate_with_search
        result = generate_with_search("test prompt")

        self.assertNotIn("error", result)
        self.assertEqual(result["text"], "Valid response text")

    @patch("core.llm.time.sleep")
    @patch("core.llm.get_client")
    def test_429_exhausted_retries_has_error_flag(self, mock_get_client, mock_sleep):
        """When 429 retries are exhausted, the error flag should be set."""
        mock_client = MagicMock()
        exc = Exception("429 Resource has been exhausted")
        exc.code = 429
        mock_client.models.generate_content.side_effect = exc
        mock_get_client.return_value = mock_client

        from core.llm import generate_with_search
        result = generate_with_search("test prompt")

        self.assertTrue(result.get("error"))
        self.assertEqual(result.get("http_status"), 429)

    @patch("core.llm.time.sleep")
    @patch("core.llm.get_client")
    def test_502_is_retried_then_error_flag(self, mock_get_client, mock_sleep):
        """502 Bad Gateway is retryable; when exhausted, it retries then sets the error flag."""
        mock_client = MagicMock()
        exc = Exception("502 Bad Gateway")
        exc.code = 502
        mock_client.models.generate_content.side_effect = exc
        mock_get_client.return_value = mock_client

        from core.llm import generate_with_search
        result = generate_with_search("test prompt")

        self.assertTrue(result.get("error"))
        self.assertEqual(result.get("http_status"), 502)
        self.assertTrue(mock_sleep.called)  # proves 502 now takes the retry path


class TestSpecialistErrorDetection(unittest.TestCase):
    """Verify specialists return error strings (not API error text) on failure."""

    @patch("agents.specialists.genetics.generate_with_search")
    @patch("core.prompt_factory.PromptFactory.build_specialist_prompt", return_value="prompt")
    def test_genetics_returns_error_on_api_failure(self, mock_prompt, mock_llm):
        mock_llm.return_value = {"text": "HTTP 429: Error communicating", "usage": {}, "error": True}
        ctx = MagicMock()

        from agents.specialists.genetics import generate_genetics_report
        result = generate_genetics_report(ctx)

        self.assertEqual(result, "Error: Failed to generate genetics report.")

    @patch("agents.specialists.biochemistry.generate_with_search")
    @patch("core.prompt_factory.PromptFactory.build_specialist_prompt", return_value="prompt")
    def test_biochemistry_returns_error_on_api_failure(self, mock_prompt, mock_llm):
        mock_llm.return_value = {"text": "HTTP 503: Error communicating", "usage": {}, "error": True}
        ctx = MagicMock()

        from agents.specialists.biochemistry import generate_biochemistry_report
        result = generate_biochemistry_report(ctx)

        self.assertEqual(result, "Error: Failed to generate biochemistry report.")

    @patch("agents.specialists.biophysics.generate_with_search")
    @patch("core.prompt_factory.PromptFactory.build_specialist_prompt", return_value="prompt")
    def test_biophysics_returns_error_on_api_failure(self, mock_prompt, mock_llm):
        mock_llm.return_value = {"text": "HTTP 504: Error communicating", "usage": {}, "error": True}
        ctx = MagicMock()

        from agents.specialists.biophysics import generate_biophysics_report
        result = generate_biophysics_report(ctx)

        self.assertEqual(result, "Error: Failed to generate biophysics report.")

    @patch("agents.specialists.genetics.generate_with_search")
    @patch("core.prompt_factory.PromptFactory.build_specialist_prompt", return_value="prompt")
    def test_genetics_returns_text_on_success(self, mock_prompt, mock_llm):
        mock_llm.return_value = {"text": "Real genetics report content", "usage": {}}
        ctx = MagicMock()

        from agents.specialists.genetics import generate_genetics_report
        result = generate_genetics_report(ctx)

        self.assertEqual(result, "Real genetics report content")


class TestPIErrorDetection(unittest.TestCase):
    """Verify PI report detects error responses."""

    @patch("agents.pi.generate_with_search")
    @patch("core.prompt_factory.PromptFactory.build_pi_prompt", return_value="prompt")
    def test_pi_returns_error_on_api_failure(self, mock_prompt, mock_llm):
        mock_llm.return_value = {"text": "HTTP 429: Error communicating", "usage": {}, "error": True}
        ctx = MagicMock()
        ctx.drug_name = "TestDrug"

        from agents.pi import generate_pi_report
        text, data = generate_pi_report(ctx, "gen", "bio", "phys")

        self.assertIn("Error", text)
        self.assertEqual(data, {})


class TestIngestionErrorDetection(unittest.TestCase):
    """Verify ingestion _fetch_page detects error responses."""

    @patch("core.ingestion.generate_with_search")
    def test_fetch_page_detects_error_response(self, mock_llm):
        mock_llm.return_value = {"text": "HTTP 503: Error communicating", "usage": {}, "error": True}

        from core.ingestion import _fetch_page
        result = _fetch_page("test prompt")

        self.assertIn("error", result)
        self.assertIn("503", result["error"])


class TestPipelineReviewerRetry(unittest.TestCase):
    """Verify pipeline reviewer retry logic skips error responses."""

    @patch('core.pipeline.resolve_paper_citations', side_effect=lambda text, _: text)
    @patch('core.markdown_utils.sanitize_markdown', side_effect=lambda text: text)
    @patch('core.pipeline.PromptFactory')
    @patch('core.pipeline.build_initial_context')
    @patch('core.pipeline.generate_genetics_report')
    @patch('core.pipeline.generate_biochemistry_report')
    @patch('core.pipeline.generate_biophysics_report')
    @patch('core.pipeline.generate_pi_report')
    @patch('agents.reviewers.generate_with_search')
    @patch('core.db.get_verified_papers')
    @patch('core.pipeline.save_papers_standalone')
    @patch('core.pipeline.save_analysis')
    @patch('core.pipeline.get_analysis', return_value=None)
    def test_reviewer_retries_on_error_response(self, mock_get, mock_save,
                                                 mock_save_papers, mock_get_papers,
                                                 mock_gen_search, mock_pi, mock_biophys,
                                                 mock_biochem, mock_gen, mock_build_ctx,
                                                 mock_pf, mock_sanitize, mock_resolve):
        """Reviewers should retry when generate_with_search returns an error dict."""
        from core.pipeline import run_analysis_pipeline
        from core.context import Context

        ctx = MagicMock(spec=Context)
        ctx.drug_name = "TestDrug"
        ctx.relevant_papers = [{"title": "p1", "url": "u1"}]
        ctx.verified_paper_ids = {}
        ctx.usage_metadata = {}

        mock_build_ctx.return_value = ctx
        mock_get_papers.return_value = [{"title": "p1", "url": "u1"}]
        mock_gen.return_value = "Genetics"
        mock_biochem.return_value = "Biochem"
        mock_biophys.return_value = "Biophysics"
        mock_pi.return_value = ("Draft PI", {})
        mock_pf.build_reviewer_prompt.return_value = "reviewer prompt"
        mock_pf.build_pi_prompt.return_value = "pi prompt"

        # Track calls per reviewer temperature
        r1_calls = {"n": 0}

        def gen_search_side_effect(*args, **kwargs):
            overrides = kwargs.get('generation_config_overrides', {})

            if overrides:
                temp = overrides.get('temperature')
                if temp == 0.9:
                    r1_calls["n"] += 1
                    if r1_calls["n"] <= 2:
                        return {"text": "HTTP 429: Rate limited", "usage": {}, "error": True}
                    return {"text": "Reviewer 1 OK", "usage": {}}
                elif temp == 0.05:
                    return {"text": "Reviewer 2 OK", "usage": {}}

            # PI revision
            return {"text": "Revised report", "usage": {}}

        mock_gen_search.side_effect = gen_search_side_effect

        # The PI revision lives in agents/pi.py and the reviewers in agents/reviewers.py; give them the same mocks.
        with patch('agents.pi.generate_with_search', mock_gen_search), \
             patch('agents.reviewers.PromptFactory', mock_pf), \
             patch('agents.pi.PromptFactory', mock_pf):
            result = run_analysis_pipeline("TestDrug")

        # Reviewer 1 should have retried past the errors
        self.assertIn("Reviewer #1", result["reviewer_reports"])
        self.assertIn("Reviewer #2", result["reviewer_reports"])
        # Verify R1 was called 3 times (2 errors + 1 success)
        self.assertEqual(r1_calls["n"], 3)

    @patch('core.pipeline.resolve_paper_citations', side_effect=lambda text, _: text)
    @patch('core.markdown_utils.sanitize_markdown', side_effect=lambda text: text)
    @patch('core.pipeline.PromptFactory')
    @patch('core.pipeline.build_initial_context')
    @patch('core.pipeline.generate_genetics_report')
    @patch('core.pipeline.generate_biochemistry_report')
    @patch('core.pipeline.generate_biophysics_report')
    @patch('core.pipeline.generate_pi_report')
    @patch('agents.reviewers.generate_with_search')
    @patch('core.db.get_verified_papers')
    @patch('core.pipeline.save_papers_standalone')
    @patch('core.pipeline.save_analysis')
    @patch('core.pipeline.get_analysis', return_value=None)
    def test_reviewer_null_text_response_does_not_crash(self, mock_get, mock_save,
                                                        mock_save_papers, mock_get_papers,
                                                        mock_gen_search, mock_pi, mock_biophys,
                                                        mock_biochem, mock_gen, mock_build_ctx,
                                                        mock_pf, mock_sanitize, mock_resolve):
        """A reviewer whose grounded call returns {"text": None} (empty response) must
        degrade to an error string, not crash the whole reviewer/revision loop.
        Regression for None[:100] in the reviewer's retry-logging."""
        from core.pipeline import run_analysis_pipeline
        from core.context import Context

        ctx = MagicMock(spec=Context)
        ctx.drug_name = "TestDrug"
        ctx.relevant_papers = [{"title": "p1", "url": "u1"}]
        ctx.verified_paper_ids = {}
        ctx.usage_metadata = {}

        mock_build_ctx.return_value = ctx
        mock_get_papers.return_value = [{"title": "p1", "url": "u1"}]
        mock_gen.return_value = "Genetics"
        mock_biochem.return_value = "Biochem"
        mock_biophys.return_value = "Biophysics"
        mock_pi.return_value = ("Draft PI", {})
        mock_pf.build_reviewer_prompt.return_value = "reviewer prompt"
        mock_pf.build_pi_prompt.return_value = "pi prompt"

        def gen_search_side_effect(*args, **kwargs):
            overrides = kwargs.get('generation_config_overrides', {})
            temp = overrides.get('temperature') if overrides else None
            if temp == 0.9:
                return {"text": "Reviewer 1 OK", "usage": {}}      # neutral succeeds
            if temp == 0.05:
                return {"text": None, "usage": {}}                 # critic + selectivity: empty
            return {"text": "Revised report", "usage": {}}         # PI revision

        mock_gen_search.side_effect = gen_search_side_effect

        # Must not raise (previously: 'NoneType' object is not subscriptable)
        # The PI revision lives in agents/pi.py and the reviewers in agents/reviewers.py; give them the same mocks.
        with patch('agents.pi.generate_with_search', mock_gen_search), \
             patch('agents.reviewers.PromptFactory', mock_pf), \
             patch('agents.pi.PromptFactory', mock_pf):
            result = run_analysis_pipeline("TestDrug")

        # The loop survived: R1 present, and the empty reviewers degraded to error strings
        self.assertIn("Reviewer #1", result["reviewer_reports"])
        self.assertIn("No text returned", result["reviewer_reports"].get("Reviewer #2", ""))
        self.assertIn("No text returned", result["reviewer_reports"].get("Selectivity Reviewer", ""))


class TestGlobalPauseCap(unittest.TestCase):
    """Verify the global 503 pause has a maximum cumulative timeout."""

    def setUp(self):
        import core.llm as llm_module
        self._orig_pause_until = llm_module._pause_until
        self._orig_pause_started = llm_module._pause_started
        llm_module._pause_until = 0
        llm_module._pause_started = 0

    def tearDown(self):
        import core.llm as llm_module
        llm_module._pause_until = self._orig_pause_until
        llm_module._pause_started = self._orig_pause_started

    def test_pause_cap_prevents_indefinite_extension(self):
        """Repeated 503 triggers should not extend pause beyond the max cap."""
        import core.llm as llm_module
        from core.llm import _trigger_global_pause

        # First trigger sets the baseline
        _trigger_global_pause()
        first_pause = llm_module._pause_until

        # Simulate time passing (halfway through pause)
        llm_module._pause_until = llm_module._pause_started + 100

        # Trigger again — should extend but not beyond cap
        _trigger_global_pause()
        second_pause = llm_module._pause_until

        max_allowed = llm_module._pause_started + llm_module._MAX_TOTAL_PAUSE_SECONDS
        self.assertLessEqual(second_pause, max_allowed)

    def test_pause_cap_refuses_after_max(self):
        """After reaching the cap, _trigger_global_pause should not extend further."""
        import core.llm as llm_module
        from core.llm import _trigger_global_pause

        now = time.time()
        # Simulate that we've already been pausing for the max duration
        llm_module._pause_started = now - llm_module._MAX_TOTAL_PAUSE_SECONDS - 1
        llm_module._pause_until = now + 10  # Still "active" but past cap

        old_pause = llm_module._pause_until
        _trigger_global_pause()

        # Should not have extended
        self.assertEqual(llm_module._pause_until, old_pause)


if __name__ == '__main__':
    unittest.main()
