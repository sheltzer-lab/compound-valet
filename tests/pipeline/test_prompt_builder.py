"""Tests for core/prompt_builder.py"""

import pytest
from unittest.mock import patch
from core.prompt_builder import format_papers_for_prompt, inject_verified_sources


# ── format_papers_for_prompt ────────────────────────────────────────

class TestFormatPapersForPrompt:
    def test_basic(self):
        papers = [
            {"title": "Paper One", "year": "2023", "url": "https://example.com/1"},
        ]
        result = format_papers_for_prompt(papers)
        assert "Paper One" in result
        assert "2023" in result
        assert "https://example.com/1" in result

    def test_empty_list(self):
        assert format_papers_for_prompt([]) == "No papers available."

    def test_unknown_year_variants(self):
        for year_val in [None, "unknown", "None", "nan", ""]:
            papers = [{"title": "P", "year": year_val, "url": "#"}]
            result = format_papers_for_prompt(papers)
            assert "????" in result

    def test_missing_fields_default(self):
        papers = [{}]
        result = format_papers_for_prompt(papers)
        assert "Unknown" in result
        assert "#" in result

    def test_numbering(self):
        papers = [
            {"title": "First", "year": "2020", "url": "#"},
            {"title": "Second", "year": "2021", "url": "#"},
        ]
        result = format_papers_for_prompt(papers)
        assert result.startswith("1.")
        assert "2." in result


# ── inject_verified_sources ─────────────────────────────────────────

class TestInjectVerifiedSources:
    def test_placeholder_replaced(self):
        with patch("core.prompt_builder.get_verified_papers") as mock_get:
            mock_get.return_value = [
                {"title": "Verified Paper", "year": "2022", "url": "https://example.com/v"},
            ]
            result = inject_verified_sources("Prompt {{verified_sources}} end", "Aspirin")
        assert "Verified Paper" in result
        assert "{{verified_sources}}" not in result

    def test_empty_papers(self):
        with patch("core.prompt_builder.get_verified_papers", return_value=[]):
            result = inject_verified_sources("{{verified_sources}}", "NoSuchDrug")
        assert "No verified sources available" in result

    def test_paper_id_format(self):
        with patch("core.prompt_builder.get_verified_papers") as mock_get:
            mock_get.return_value = [
                {"title": "Paper A", "year": "2021", "url": "https://example.com/a"},
            ]
            result = inject_verified_sources("{{verified_sources}}", "Imatinib")
        assert "Imatinib-Paper-1" in result

    def test_multiple_papers_numbered(self):
        with patch("core.prompt_builder.get_verified_papers") as mock_get:
            mock_get.return_value = [
                {"title": "A", "year": "2020", "url": "#"},
                {"title": "B", "year": "2021", "url": "#"},
                {"title": "C", "year": "2022", "url": "#"},
            ]
            result = inject_verified_sources("{{verified_sources}}", "Drug")
        assert "Drug-Paper-1" in result
        assert "Drug-Paper-2" in result
        assert "Drug-Paper-3" in result
