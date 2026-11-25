"""Tests for core/references/checker.py"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from core.references.checker import ReferenceChecker, SIMILARITY_THRESHOLD


@pytest.fixture
def checker():
    """Create a ReferenceChecker with mocked clients."""
    with patch("core.references.checker.SemanticScholarClient"), \
         patch("core.references.checker.CrossRefClient"):
        return ReferenceChecker()


# ── _title_similarity ───────────────────────────────────────────────

class TestTitleSimilarity:
    def test_identical_titles(self, checker):
        score = checker._title_similarity("Imatinib targets BCR-ABL", "Imatinib targets BCR-ABL")
        assert score == 1.0

    def test_minor_difference(self, checker):
        score = checker._title_similarity(
            "Imatinib targets BCR-ABL kinase",
            "Imatinib targets BCR-ABL kinases",
        )
        assert score > SIMILARITY_THRESHOLD

    def test_unrelated_titles(self, checker):
        score = checker._title_similarity("Imatinib targets BCR-ABL", "Aspirin and cardiovascular risk")
        assert score < SIMILARITY_THRESHOLD

    def test_case_insensitive(self, checker):
        score = checker._title_similarity("Hello World", "hello world")
        assert score == 1.0

    def test_diacritics_normalized(self, checker):
        score = checker._title_similarity("Müller et al. study", "Muller et al study")
        assert score > 0.9

    def test_empty_title1(self, checker):
        assert checker._title_similarity("", "Something") == 0.0

    def test_empty_title2(self, checker):
        assert checker._title_similarity("Something", "") == 0.0

    def test_both_empty(self, checker):
        assert checker._title_similarity("", "") == 0.0


# ── _extract_metadata_ss ───────────────────────────────────────────

class TestExtractMetadataSS:
    def test_full_paper(self, checker):
        paper = {
            "title": "Test Paper",
            "authors": [{"name": "Jane Smith"}, {"name": "Bob Jones"}],
            "year": 2023,
        }
        meta = checker._extract_metadata_ss(paper)
        assert meta["title"] == "Test Paper"
        assert meta["authors"] == ["Jane Smith", "Bob Jones"]
        assert meta["year"] == 2023
        assert meta["citation"] == "Smith, 2023"

    def test_no_authors(self, checker):
        paper = {"title": "Orphan Paper", "authors": [], "year": 2020}
        meta = checker._extract_metadata_ss(paper)
        assert meta["authors"] == []
        # Falls back to title
        assert meta["citation"] == "Orphan Paper"

    def test_no_year(self, checker):
        paper = {"title": "No Year", "authors": [{"name": "Alice"}]}
        meta = checker._extract_metadata_ss(paper)
        assert meta["year"] is None
        assert meta["citation"] == "Alice"

    def test_title_fallback_when_none(self, checker):
        paper = {"title": None, "authors": [], "year": None}
        meta = checker._extract_metadata_ss(paper)
        assert meta["title"] == "Untitled"

    def test_long_title_truncated_in_citation(self, checker):
        long_title = "A" * 100
        paper = {"title": long_title, "authors": [], "year": None}
        meta = checker._extract_metadata_ss(paper)
        assert meta["citation"].endswith("...")
        assert len(meta["citation"]) == 53  # 50 + "..."


# ── _extract_metadata_cr ───────────────────────────────────────────

class TestExtractMetadataCR:
    def test_full_crossref(self, checker):
        work = {
            "title": ["CrossRef Paper"],
            "author": [
                {"given": "Jane", "family": "Doe"},
                {"given": "Bob", "family": "Smith"},
            ],
            "published": {"date-parts": [[2021]]},
        }
        meta = checker._extract_metadata_cr(work)
        assert meta["title"] == "CrossRef Paper"
        assert meta["authors"] == ["Jane Doe", "Bob Smith"]
        assert meta["year"] == 2021
        assert meta["citation"] == "Doe, 2021"

    def test_date_parts_fallback_published_print(self, checker):
        work = {
            "title": ["Paper"],
            "author": [{"given": "A", "family": "B"}],
            "published-print": {"date-parts": [[2019]]},
        }
        meta = checker._extract_metadata_cr(work)
        assert meta["year"] == 2019

    def test_date_parts_fallback_published_online(self, checker):
        work = {
            "title": ["Paper"],
            "author": [{"given": "A", "family": "B"}],
            "published-online": {"date-parts": [[2018]]},
        }
        meta = checker._extract_metadata_cr(work)
        assert meta["year"] == 2018

    def test_no_authors(self, checker):
        work = {"title": ["Solo Paper"], "author": []}
        meta = checker._extract_metadata_cr(work)
        assert meta["authors"] == []
        assert meta["citation"] == "Solo Paper"

    def test_title_as_list(self, checker):
        work = {"title": ["First Title", "Second"], "author": []}
        meta = checker._extract_metadata_cr(work)
        assert meta["title"] == "First Title"

    def test_empty_title_list(self, checker):
        work = {"title": [], "author": []}
        meta = checker._extract_metadata_cr(work)
        assert meta["title"] == "Untitled"

    def test_no_date_info(self, checker):
        work = {"title": ["Paper"], "author": [{"given": "X", "family": "Y"}]}
        meta = checker._extract_metadata_cr(work)
        assert meta["year"] is None
        assert meta["citation"] == "Y"


# ── _find_best_match ───────────────────────────────────────────────

class TestFindBestMatch:
    def test_above_threshold(self, checker):
        ref = {"title": "ABC kinase inhibitor study"}
        candidates = [{"title": "ABC kinase inhibitor study"}]
        result = checker._find_best_match(ref, candidates, source="semantic_scholar")
        assert result is not None

    def test_below_threshold(self, checker):
        ref = {"title": "ABC kinase inhibitor study"}
        candidates = [{"title": "Completely unrelated paper on geology"}]
        result = checker._find_best_match(ref, candidates, source="semantic_scholar")
        assert result is None

    def test_empty_candidates(self, checker):
        ref = {"title": "Any title"}
        result = checker._find_best_match(ref, [], source="semantic_scholar")
        assert result is None

    def test_picks_highest_score(self, checker):
        ref = {"title": "Target validation for kinase X"}
        candidates = [
            {"title": "Unrelated paper"},
            {"title": "Target validation for kinase X in cancer"},
        ]
        result = checker._find_best_match(ref, candidates, source="semantic_scholar")
        assert result["title"] == "Target validation for kinase X in cancer"

    def test_crossref_title_format(self, checker):
        ref = {"title": "Specific pharmacology paper"}
        candidates = [{"title": ["Specific pharmacology paper"]}]
        result = checker._find_best_match(ref, candidates, source="crossref")
        assert result is not None

    def test_empty_ref_title(self, checker):
        ref = {"title": ""}
        candidates = [{"title": "Something"}]
        result = checker._find_best_match(ref, candidates, source="semantic_scholar")
        assert result is None


# ── _create_result ──────────────────────────────────────────────────

class TestCreateResult:
    def test_verified_shape(self, checker):
        ref = {"title": "Paper", "year": "2023"}
        result = checker._create_result(
            ref, verified=True, url="https://example.com",
            source="Semantic Scholar",
            verified_match={"title": "Paper", "authors": ["A"]}
        )
        assert result["verified"] is True
        assert result["url"] == "https://example.com"
        assert result["source"] == "Semantic Scholar"
        assert result["reference"] is ref
        assert result["error"] is None

    def test_failed_shape(self, checker):
        ref = {"title": "Bad Paper"}
        result = checker._create_result(ref, verified=False, error="Not found")
        assert result["verified"] is False
        assert result["url"] is None
        assert result["source"] is None
        assert result["error"] == "Not found"


# ── _truncate_title_at_separator ──────────────────────────────────

class TestTruncateTitleAtSeparator:
    def test_short_title_returns_none(self, checker):
        assert checker._truncate_title_at_separator("Short title") is None

    def test_exactly_100_returns_none(self, checker):
        title = "A" * 100
        assert checker._truncate_title_at_separator(title) is None

    def test_colon_separator(self, checker):
        main = "A" * 60
        title = f"{main}: subtitle that makes it long enough to exceed the threshold easily"
        result = checker._truncate_title_at_separator(title)
        assert result == main

    def test_dash_separator(self, checker):
        main = "A" * 60
        title = f"{main} - subtitle that makes it long enough to exceed the threshold easily"
        result = checker._truncate_title_at_separator(title)
        assert result == main

    def test_em_dash_separator(self, checker):
        main = "A" * 60
        title = f"{main} — subtitle that makes it long enough to exceed the threshold easily"
        result = checker._truncate_title_at_separator(title)
        assert result == main

    def test_picks_last_separator(self, checker):
        # Two colons — should pick the last one that leaves ≥30 chars
        title = "Part one: part two which is quite long: part three making it over one hundred characters total easily"
        result = checker._truncate_title_at_separator(title)
        assert result == "Part one: part two which is quite long"

    def test_no_separator_hard_truncates_on_word_boundary(self, checker):
        # No separator chars, just a long title
        title = "This is a very long title without any separators that keeps going and going well past one hundred characters total"
        result = checker._truncate_title_at_separator(title)
        # Should truncate at word boundary within first 100 chars
        assert len(result) <= 100
        assert not result.endswith(' ')
        assert result == title[:100][:title[:100].rfind(' ')]

    def test_separator_too_early_ignored(self, checker):
        # Separator at position 10 — leaves only 10 chars, below 30-char minimum
        title = "Short part: " + "x" * 120
        result = checker._truncate_title_at_separator(title)
        # Should NOT truncate at the colon (only 10 chars before it)
        # Falls through to hard-truncate at 100 chars
        assert len(result) <= 100


# ── _check_crossref_truncated (integration) ───────────────────────

class TestTruncatedCrossrefFallback:
    """Integration tests for the truncated title fallback path."""

    LONG_TITLE = (
        "NH125 is a potent and selective inhibitor of eukaryotic elongation factor 2 kinase"
        ": a cautionary note on the use of this compound in cancer research"
    )
    MAIN_TITLE = "NH125 is a potent and selective inhibitor of eukaryotic elongation factor 2 kinase"

    def _make_crossref_hit(self, title, year=2015, family="Chen"):
        return {
            "title": [title],
            "author": [{"given": "Z", "family": family}],
            "published": {"date-parts": [[year]]},
            "DOI": "10.1234/fake",
            "URL": "https://doi.org/10.1234/fake",
        }

    def _run(self, coro):
        return asyncio.run(coro)

    def test_truncated_fallback_verifies(self, checker):
        """Full title fails normal match; truncated title succeeds."""
        ref = {"title": self.LONG_TITLE, "year": 2015, "authors": ["Z Chen"]}
        hit = self._make_crossref_hit(self.MAIN_TITLE, year=2015, family="Chen")

        checker.cr_client.search_works = AsyncMock(return_value=[hit])
        checker._resolve_best_url = AsyncMock(return_value="https://doi.org/10.1234/fake")

        async def run():
            import aiohttp
            async with aiohttp.ClientSession() as session:
                return await checker._check_crossref_truncated(ref, session)

        result = self._run(run())
        assert result is not None
        assert result["verified"] is True
        # Original reference is preserved
        assert result["reference"]["title"] == self.LONG_TITLE

    def test_truncated_fallback_rejects_year_mismatch(self, checker):
        """Truncated match rejected when year doesn't corroborate."""
        ref = {"title": self.LONG_TITLE, "year": 2015, "authors": ["Z Chen"]}
        hit = self._make_crossref_hit(self.MAIN_TITLE, year=2020, family="Chen")

        checker.cr_client.search_works = AsyncMock(return_value=[hit])
        checker._resolve_best_url = AsyncMock(return_value="https://doi.org/10.1234/fake")

        async def run():
            import aiohttp
            async with aiohttp.ClientSession() as session:
                return await checker._check_crossref_truncated(ref, session)

        result = self._run(run())
        assert result is None

    def test_truncated_fallback_rejects_author_mismatch(self, checker):
        """Truncated match rejected when no author last names match."""
        ref = {"title": self.LONG_TITLE, "year": 2015, "authors": ["Z Chen"]}
        hit = self._make_crossref_hit(self.MAIN_TITLE, year=2015, family="Yamamoto")

        checker.cr_client.search_works = AsyncMock(return_value=[hit])
        checker._resolve_best_url = AsyncMock(return_value="https://doi.org/10.1234/fake")

        async def run():
            import aiohttp
            async with aiohttp.ClientSession() as session:
                return await checker._check_crossref_truncated(ref, session)

        result = self._run(run())
        assert result is None

    def test_truncated_fallback_accepts_without_corroboration_fields(self, checker):
        """When ref has neither year nor authors, accept on title match alone."""
        ref = {"title": self.LONG_TITLE}
        hit = self._make_crossref_hit(self.MAIN_TITLE, year=2015, family="Chen")

        checker.cr_client.search_works = AsyncMock(return_value=[hit])
        checker._resolve_best_url = AsyncMock(return_value="https://doi.org/10.1234/fake")

        async def run():
            import aiohttp
            async with aiohttp.ClientSession() as session:
                return await checker._check_crossref_truncated(ref, session)

        result = self._run(run())
        assert result is not None
        assert result["verified"] is True
