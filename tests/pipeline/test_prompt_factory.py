"""Tests for core/prompt_factory.py"""

import pytest
from unittest.mock import patch, MagicMock
from core.prompt_factory import PromptFactory


FAKE_TEMPLATE = "# Objective\nSearch for {{drug_name}}"
FAKE_SPECIALIST = "Context: {{agent_context}}\n{{verified_sources}}"
FAKE_PI_PERSONA = "You are a PI."
FAKE_PI_DRAFT = "{{pi_persona}}\nDrug: {{drug_name}}\n{{context_summary}}\n{{genetics_report}}\n{{biochem_report}}\n{{biophysics_report}}\n{{verified_sources}}\n{{pi_report_format}}"
FAKE_PI_REVISION = "{{pi_persona}}\nDrug: {{drug_name}}\n{{context_summary}}\n{{genetics_report}}\n{{biochem_report}}\n{{biophysics_report}}\n{{verified_sources}}\n{{pi_report_format}}\nFeedback: {{feedback}}\nR1: {{reviewer1_feedback}}\nR2: {{reviewer2_feedback}}\nSel: {{reviewer_selectivity_feedback}}\n{{missing_papers_section}}\n{{initial_pi_report}}"
FAKE_PI_REPORT_FORMAT = "FORMAT_BLOCK"
FAKE_REVIEWER_CRITIC = "{{pi_persona}}\n{{context_summary}}\n{{pi_report}}\n{{verified_sources}}"
FAKE_REVIEWER_NEUTRAL = "NEUTRAL: {{pi_persona}}\n{{context_summary}}\n{{pi_report}}\n{{verified_sources}}"
FAKE_REVIEWER_SELECTIVITY = "SELECTIVITY: {{pi_persona}}\n{{context_summary}}\n{{specialist_reports}}\n{{pi_report}}\n{{verified_sources}}"


def _mock_load(path):
    """Return fake template based on the path suffix."""
    if "literature_search" in path:
        return FAKE_TEMPLATE
    if "genetics_prompt" in path:
        return FAKE_SPECIALIST
    if "biochemistry_prompt" in path:
        return FAKE_SPECIALIST
    if "biophysics_prompt" in path:
        return FAKE_SPECIALIST
    if "pi_persona" in path:
        return FAKE_PI_PERSONA
    if "pi_draft" in path:
        return FAKE_PI_DRAFT
    if "pi_revision" in path:
        return FAKE_PI_REVISION
    if "pi_report_format" in path:
        return FAKE_PI_REPORT_FORMAT
    if "reviewer_critic" in path:
        return FAKE_REVIEWER_CRITIC
    if "reviewer_neutral" in path:
        return FAKE_REVIEWER_NEUTRAL
    if "reviewer_selectivity" in path:
        return FAKE_REVIEWER_SELECTIVITY
    return ""


@pytest.fixture(autouse=True)
def mock_templates():
    with patch.object(PromptFactory, "_load_template", side_effect=_mock_load):
        yield


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.drug_name = "TestDrug"
    ctx.get_agent_context.return_value = "AGENT_CONTEXT_BLOCK"
    ctx.get_minimal_context.return_value = "MINIMAL_CONTEXT"
    return ctx


# ── build_literature_search_prompt ──────────────────────────────────

class TestBuildLiteratureSearchPrompt:
    def test_drug_name_injected(self):
        prompt = PromptFactory.build_literature_search_prompt("Imatinib")
        assert "Imatinib" in prompt

    def test_angle_injected(self):
        angle = {"name": "Mechanism", "instruction": "Focus on biochemistry"}
        prompt = PromptFactory.build_literature_search_prompt("X", angle=angle)
        assert "FOCUS AREA: Mechanism" in prompt
        assert "Focus on biochemistry" in prompt

    def test_no_angle(self):
        prompt = PromptFactory.build_literature_search_prompt("X")
        assert "FOCUS AREA" not in prompt


# ── build_all_search_prompts ────────────────────────────────────────

class TestBuildAllSearchPrompts:
    def test_returns_three(self):
        results = PromptFactory.build_all_search_prompts("TestDrug")
        assert len(results) == 3

    def test_different_angles(self):
        results = PromptFactory.build_all_search_prompts("TestDrug")
        angles = [r["angle"]["name"] for r in results]
        assert angles == ["Mechanism", "Genetics", "Clinical"]

    def test_each_has_prompt(self):
        results = PromptFactory.build_all_search_prompts("TestDrug")
        for r in results:
            assert "TestDrug" in r["prompt"]


# ── build_specialist_prompt ─────────────────────────────────────────

class TestBuildSpecialistPrompt:
    def test_context_injected(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_specialist_prompt("genetics", mock_context)
        assert "AGENT_CONTEXT_BLOCK" in prompt

    def test_source_injection_toggle_off(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources") as mock_inject:
            PromptFactory.build_specialist_prompt("genetics", mock_context, inject_sources=False)
        mock_inject.assert_not_called()

    def test_source_injection_toggle_on(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p) as mock_inject:
            PromptFactory.build_specialist_prompt("genetics", mock_context, inject_sources=True)
        mock_inject.assert_called_once()

    def test_invalid_name_raises(self, mock_context):
        with pytest.raises(ValueError, match="Unknown specialist"):
            PromptFactory.build_specialist_prompt("astrology", mock_context)


# ── build_pi_prompt ─────────────────────────────────────────────────

class TestBuildPiPrompt:
    def test_draft_mode(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_pi_prompt(
                "TestDrug", mock_context,
                {"genetics": "G_REPORT", "biochemistry": "B_REPORT", "biophysics": "P_REPORT"},
            )
        assert "You are a PI." in prompt
        assert "TestDrug" in prompt
        assert "G_REPORT" in prompt
        assert "B_REPORT" in prompt
        assert "P_REPORT" in prompt

    def test_revision_mode_dict_feedback(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_pi_prompt(
                "TestDrug", mock_context,
                {"genetics": "G", "biochemistry": "B", "biophysics": "P"},
                feedback={"Reviewer #1": "Good", "Reviewer #2": "Needs work"},
            )
        assert "Good" in prompt
        assert "Needs work" in prompt

    def test_revision_mode_selectivity_feedback(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_pi_prompt(
                "TestDrug", mock_context,
                {"genetics": "G", "biochemistry": "B", "biophysics": "P"},
                feedback={"Reviewer #1": "Good", "Reviewer #2": "Needs work",
                          "Selectivity Reviewer": "Grade is under-called: MODERATE"},
            )
        assert "Grade is under-called: MODERATE" in prompt

    def test_revision_mode_string_feedback(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_pi_prompt(
                "TestDrug", mock_context,
                {"genetics": "G", "biochemistry": "B", "biophysics": "P"},
                feedback="Single reviewer feedback text",
            )
        assert "Single reviewer feedback text" in prompt

    def test_specialist_reports_missing_defaults(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_pi_prompt(
                "TestDrug", mock_context, {},
            )
        assert "Missing" in prompt

    def test_persona_included(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_pi_prompt(
                "TestDrug", mock_context,
                {"genetics": "G", "biochemistry": "B", "biophysics": "P"},
            )
        assert "You are a PI." in prompt


# ── build_reviewer_prompt ───────────────────────────────────────────

class TestBuildReviewerPrompt:
    def test_critic_template(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_reviewer_prompt(
                "TestDrug", mock_context, "DRAFT_REPORT", reviewer_type="critic"
            )
        assert "DRAFT_REPORT" in prompt
        # Critic template doesn't start with "NEUTRAL:"
        assert not prompt.startswith("NEUTRAL:")

    def test_neutral_template(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_reviewer_prompt(
                "TestDrug", mock_context, "DRAFT_REPORT", reviewer_type="neutral"
            )
        assert prompt.startswith("NEUTRAL:")

    def test_pi_report_replaced(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_reviewer_prompt(
                "TestDrug", mock_context, "MY_PI_REPORT"
            )
        assert "MY_PI_REPORT" in prompt

    def test_selectivity_template(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_reviewer_prompt(
                "TestDrug", mock_context, "DRAFT_REPORT", reviewer_type="selectivity",
                specialist_reports={"genetics": "G_SPEC", "biochemistry": "B_SPEC",
                                    "biophysics": "P_SPEC"},
            )
        assert prompt.startswith("SELECTIVITY:")
        assert "DRAFT_REPORT" in prompt
        # Specialist reports are injected so it can see panels the specialists flagged
        assert "B_SPEC" in prompt
        assert "G_SPEC" in prompt

    def test_selectivity_specialist_reports_default_missing(self, mock_context):
        with patch("core.prompt_factory.inject_verified_sources", side_effect=lambda p, d, j=None: p):
            prompt = PromptFactory.build_reviewer_prompt(
                "TestDrug", mock_context, "DRAFT_REPORT", reviewer_type="selectivity",
            )
        assert "Missing" in prompt


# ── validate_no_unreplaced_placeholders ─────────────────────────────

class TestValidateNoUnreplacedPlaceholders:
    def test_clean_prompt(self):
        assert PromptFactory.validate_no_unreplaced_placeholders("No placeholders here") == []

    def test_single_unreplaced(self):
        result = PromptFactory.validate_no_unreplaced_placeholders("Hello {{world}}")
        assert result == ["world"]

    def test_multiple_unreplaced(self):
        result = PromptFactory.validate_no_unreplaced_placeholders("{{a}} and {{b}}")
        assert set(result) == {"a", "b"}


# ── assert_prompt_complete ──────────────────────────────────────────

class TestAssertPromptComplete:
    def test_passes_clean(self):
        PromptFactory.assert_prompt_complete("All good")  # should not raise

    def test_raises_on_leftover(self):
        with pytest.raises(ValueError, match="Unreplaced placeholders"):
            PromptFactory.assert_prompt_complete("Still has {{drug_name}}")

    def test_context_in_error(self):
        with pytest.raises(ValueError, match="pi prompt"):
            PromptFactory.assert_prompt_complete("{{x}}", context="pi prompt")


class TestRealPromptTemplates:
    """Verify the real prompts/*.md templates have no unreplaced placeholders."""

    @pytest.mark.parametrize("specialist", ["genetics", "biochemistry", "biophysics"])
    def test_specialist_real_template_complete(self, specialist):
        from core.context import Context
        ctx = Context(drug_name="Imatinib", relevant_papers=[], verified_paper_ids={})
        with patch.object(PromptFactory, "_load_template", wraps=lambda p: open(p).read()):
            with patch("core.prompt_factory.inject_verified_sources",
                       side_effect=lambda p, d, j=None: p.replace("{{verified_sources}}", "SOURCES")):
                prompt = PromptFactory.build_specialist_prompt(specialist, ctx)
        assert PromptFactory.validate_no_unreplaced_placeholders(prompt) == []
        assert "Imatinib" in prompt

    def test_literature_search_and_pi_real_templates_complete(self):
        from core.context import Context
        ctx = Context(drug_name="Imatinib", relevant_papers=[], verified_paper_ids={})
        reports = {"genetics": "G", "biochemistry": "BC", "biophysics": "BP"}
        with patch.object(PromptFactory, "_load_template", wraps=lambda p: open(p).read()):
            with patch("core.prompt_factory.inject_verified_sources",
                       side_effect=lambda p, d, j=None: p.replace("{{verified_sources}}", "SOURCES")):
                for angle in PromptFactory.SEARCH_ANGLES:
                    p = PromptFactory.build_literature_search_prompt("Imatinib", angle)
                    assert PromptFactory.validate_no_unreplaced_placeholders(p) == []
                draft = PromptFactory.build_pi_prompt("Imatinib", ctx, reports)
                assert PromptFactory.validate_no_unreplaced_placeholders(draft) == []
                rev = PromptFactory.build_pi_prompt(
                    "Imatinib", ctx, reports,
                    feedback={"Reviewer #1": "R1", "Reviewer #2": "R2", "Selectivity Reviewer": "RS"},
                    initial_report="DRAFT",
                    missing_papers_section="",
                )
                assert PromptFactory.validate_no_unreplaced_placeholders(rev) == []
