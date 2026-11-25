"""Tests for core/references/utils.py"""

import pytest
from unittest.mock import patch
from core.references.utils import (
    normalize_text,
    clean_title_for_search,
    is_binding_db_link,
    is_safe_url,
)


# ── normalize_text ──────────────────────────────────────────────────

class TestNormalizeText:
    def test_empty_string(self):
        assert normalize_text("") == ""

    def test_none_returns_empty(self):
        assert normalize_text(None) == ""

    def test_unicode_diacritics_stripped(self):
        assert normalize_text("Müller") == "muller"

    def test_special_chars_removed(self):
        assert normalize_text("hello-world!") == "helloworld"

    def test_whitespace_collapsed(self):
        assert normalize_text("  foo   bar  ") == "foo bar"

    def test_lowercased(self):
        assert normalize_text("HELLO World") == "hello world"

    def test_combined_normalization(self):
        result = normalize_text("  Ångström — café  ")
        assert result == "angstrom cafe"


# ── clean_title_for_search ──────────────────────────────────────────

class TestCleanTitleForSearch:
    def test_empty_string(self):
        assert clean_title_for_search("") == ""

    def test_none_returns_empty(self):
        assert clean_title_for_search(None) == ""

    def test_newlines_replaced(self):
        assert clean_title_for_search("line1\nline2") == "line1 line2"

    def test_whitespace_collapsed(self):
        assert clean_title_for_search("  foo   bar  ") == "foo bar"

    def test_preserves_punctuation(self):
        # Unlike normalize_text, this doesn't strip punctuation
        assert ":" in clean_title_for_search("Title: Subtitle")


# ── is_binding_db_link ──────────────────────────────────────────────

class TestIsBindingDbLink:
    def test_empty_string(self):
        assert is_binding_db_link("") is False

    def test_none(self):
        assert is_binding_db_link(None) is False

    @pytest.mark.parametrize("domain", [
        "bindingdb.org",
        "ebi.ac.uk/chembl",
        "pubchem.ncbi.nlm.nih.gov",
        "uniprot.org",
        "rcsb.org",
        "wwpdb.org",
        "drugbank.com",
        "drugbank.ca",
        "semanticscholar.org",
    ])
    def test_blocked_domains(self, domain):
        assert is_binding_db_link(f"https://{domain}/some/path") is True

    def test_case_insensitive(self):
        assert is_binding_db_link("https://BINDINGDB.ORG/page") is True

    def test_doi_pdb_pattern_blocked(self):
        assert is_binding_db_link("https://doi.org/10.1234/pdb") is True

    def test_doi_non_pdb_allowed(self):
        assert is_binding_db_link("https://doi.org/10.1234/paper") is False

    def test_allowed_url(self):
        assert is_binding_db_link("https://pubmed.ncbi.nlm.nih.gov/12345/") is False


# ── is_safe_url ─────────────────────────────────────────────────────

class TestIsSafeUrl:
    def test_valid_https(self):
        with patch("core.references.utils.resolve_doi_via_crossref_json", return_value="https://doi.org/10.1234/ok"):
            assert is_safe_url("https://example.com/paper") is True

    def test_blocked_domain(self):
        assert is_safe_url("https://bindingdb.org/page") is False

    def test_no_protocol(self):
        assert is_safe_url("example.com/paper") is False

    def test_empty_string(self):
        assert is_safe_url("") is False

    def test_none(self):
        assert is_safe_url(None) is False

    def test_doi_resolving_to_blocked_rejected(self):
        with patch(
            "core.references.utils.resolve_doi_via_crossref_json",
            return_value="https://bindingdb.org/entry",
        ):
            assert is_safe_url("https://doi.org/10.1234/foo") is False

    def test_doi_resolving_to_safe_accepted(self):
        with patch(
            "core.references.utils.resolve_doi_via_crossref_json",
            return_value="https://nature.com/article",
        ):
            assert is_safe_url("https://doi.org/10.1234/foo") is True
