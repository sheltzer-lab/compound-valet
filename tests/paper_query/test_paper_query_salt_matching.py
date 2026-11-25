import unittest
from unittest.mock import patch
from core.db import normalize_key, strip_salt_suffix


class TestStripSaltSuffix(unittest.TestCase):
    """Unit tests for strip_salt_suffix()."""

    # --- HCl variants ---
    def test_hcl(self):
        self.assertEqual(strip_salt_suffix("EHT 1864 HCl"), "EHT 1864")

    def test_2hcl(self):
        self.assertEqual(strip_salt_suffix("EHT 1864 2HCl"), "EHT 1864")

    def test_hydrochloride(self):
        self.assertEqual(strip_salt_suffix("Erlotinib hydrochloride"), "Erlotinib")

    def test_dihydrochloride(self):
        self.assertEqual(strip_salt_suffix("Drug dihydrochloride"), "Drug")

    # --- Other common salts ---
    def test_sulfate(self):
        self.assertEqual(strip_salt_suffix("Vinblastine sulfate"), "Vinblastine")

    def test_acetate(self):
        self.assertEqual(strip_salt_suffix("Abiraterone acetate"), "Abiraterone")

    def test_mesylate(self):
        self.assertEqual(strip_salt_suffix("Imatinib mesylate"), "Imatinib")

    def test_maleate(self):
        self.assertEqual(strip_salt_suffix("Sunitinib maleate"), "Sunitinib")

    def test_tfa(self):
        self.assertEqual(strip_salt_suffix("Drug TFA"), "Drug")

    def test_sodium(self):
        self.assertEqual(strip_salt_suffix("Diclofenac sodium"), "Diclofenac")

    def test_free_base(self):
        self.assertEqual(strip_salt_suffix("RGB-286638 free base"), "RGB-286638")

    def test_sodium_salt(self):
        self.assertEqual(strip_salt_suffix("Drug sodium salt"), "Drug")

    def test_tartrate(self):
        self.assertEqual(strip_salt_suffix("Drug tartrate"), "Drug")

    def test_fumarate(self):
        self.assertEqual(strip_salt_suffix("Drug fumarate"), "Drug")

    def test_citrate(self):
        self.assertEqual(strip_salt_suffix("Drug citrate"), "Drug")

    def test_tosylate(self):
        self.assertEqual(strip_salt_suffix("Drug tosylate"), "Drug")

    # --- Hydrates ---
    def test_monohydrate(self):
        self.assertEqual(strip_salt_suffix("Drug monohydrate"), "Drug")

    def test_heptahydrate(self):
        self.assertEqual(strip_salt_suffix("Drug heptahydrate"), "Drug")

    def test_hydrate(self):
        self.assertEqual(strip_salt_suffix("Drug hydrate"), "Drug")

    # --- Compound (iterative) stripping ---
    def test_hcl_monohydrate(self):
        result = strip_salt_suffix("Drug HCl monohydrate")
        self.assertEqual(normalize_key(result), normalize_key("Drug"))

    def test_sodium_trihydrate(self):
        result = strip_salt_suffix("Drug sodium trihydrate")
        self.assertEqual(normalize_key(result), normalize_key("Drug"))

    # --- No-op cases ---
    def test_no_suffix(self):
        self.assertEqual(strip_salt_suffix("Imatinib"), "Imatinib")

    def test_empty(self):
        self.assertEqual(strip_salt_suffix(""), "")

    def test_none(self):
        self.assertIsNone(strip_salt_suffix(None))

    # --- Safety: short base after strip ---
    def test_short_base_preserved(self):
        # "Na chloride" -> base "Na" is too short (< 3 normalized chars)
        self.assertEqual(strip_salt_suffix("Na chloride"), "Na chloride")

    def test_two_char_base_preserved(self):
        self.assertEqual(strip_salt_suffix("AB HCl"), "AB HCl")

    # --- Separator required ---
    def test_no_space_no_strip(self):
        # "DrugHCl" should NOT be stripped (no separator before suffix)
        self.assertEqual(strip_salt_suffix("DrugHCl"), "DrugHCl")

    # --- Hyphen as separator ---
    def test_hyphen_between_alpha(self):
        # "Drug-hydrochloride" — hyphen between alpha chars treated as separator
        result = strip_salt_suffix("Drug-hydrochloride")
        self.assertEqual(normalize_key(result), normalize_key("Drug"))

    # --- Numeric prefix variants ---
    def test_dihcl(self):
        result = strip_salt_suffix("Drug diHCl")
        self.assertEqual(normalize_key(result), normalize_key("Drug"))

    def test_trihydrochloride(self):
        result = strip_salt_suffix("Drug trihydrochloride")
        self.assertEqual(normalize_key(result), normalize_key("Drug"))


class TestVerifyDrugSaltMatching(unittest.TestCase):
    """_verify_drug() salt matching through the paper drug index. The index derives the
    salt-stripped forms from the database names itself, so only the names are given here."""

    def setUp(self):
        from core.paper_query_engine import PaperQueryEngine
        self.engine = PaperQueryEngine()
        # The target match is an LLM call; these tests are about which drug is found.
        patcher = patch.object(PaperQueryEngine, "_check_target_match", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_drug_data(self, name, alternate_names=None):
        return {
            "name": name,
            "target": "Some target",
            "human_target_gene": "GENE1",
            "context": "test context",
            "alternate_names": alternate_names or [],
        }

    @staticmethod
    def _analysis(drug):
        return {
            "drug": drug,
            "structured_summary": {
                "final_verdict": "VERIFIED",
                "selectivity_grade": "SELECTIVE",
                "target": "Target",
                "target_gene_names": ["GENE1"],
            },
        }

    def _verify(self, drug_data, db_names):
        from core.paper_drug_index import DrugIndex
        return self.engine._verify_drug(drug_data, DrugIndex(db_names))

    @patch("core.paper_query_engine.get_analysis_fuzzy")
    def test_paper_base_matches_db_salt(self, mock_fuzzy):
        """Paper 'EHT 1864' matches DB 'EHT 1864 2HCl': the DB name without its salt word."""
        mock_fuzzy.return_value = self._analysis("EHT 1864 2HCl")
        result = self._verify(self._make_drug_data("EHT 1864"), ["EHT 1864 2HCl"])
        self.assertEqual(result["db_match"], "EHT 1864 2HCl")
        self.assertEqual(result["match_via"], "name-base")
        self.assertNotEqual(result["verdict_text"], "Not in Database")

    @patch("core.paper_query_engine.get_analysis_fuzzy")
    def test_paper_salt_matches_db_base(self, mock_fuzzy):
        """Paper 'Drug HCl' matches DB 'Drug': the paper name without its salt word."""
        mock_fuzzy.return_value = self._analysis("Drug")
        result = self._verify(self._make_drug_data("Drug HCl"), ["Drug"])
        self.assertEqual(result["db_match"], "Drug")

    @patch("core.paper_query_engine.get_analysis_fuzzy")
    def test_cross_salt_match(self, mock_fuzzy):
        """Paper 'Drug HCl' matches DB 'Drug 2HCl': both without their salt words are 'Drug'."""
        mock_fuzzy.return_value = self._analysis("Drug 2HCl")
        result = self._verify(self._make_drug_data("Drug HCl"), ["Drug 2HCl"])
        self.assertEqual(result["db_match"], "Drug 2HCl")

    @patch("core.paper_query_engine.get_analysis_fuzzy")
    def test_exact_match_takes_priority(self, mock_fuzzy):
        """When both 'Vinblastine' and 'Vinblastine sulfate' exist, the exact name wins."""
        mock_fuzzy.return_value = self._analysis("Vinblastine")
        result = self._verify(self._make_drug_data("Vinblastine"),
                              ["Vinblastine", "Vinblastine sulfate"])
        self.assertEqual(result["db_match"], "Vinblastine")
        self.assertEqual(result["match_via"], "name")

    @patch("core.paper_query_engine.get_analysis_fuzzy")
    def test_alternate_name_salt_stripped(self, mock_fuzzy):
        """Alternate name 'Drug mesylate' matches DB 'Drug' via salt stripping."""
        mock_fuzzy.return_value = self._analysis("Drug")
        result = self._verify(self._make_drug_data("SomeOtherName", ["Drug mesylate"]), ["Drug"])
        self.assertEqual(result["db_match"], "Drug")


if __name__ == "__main__":
    unittest.main()
