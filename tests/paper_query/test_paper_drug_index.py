"""The paper drug index and its lookup order (core/paper_drug_index.py).

Each test is one rule of the lookup order, mostly with the real case that the rule exists for.
"""
import unittest
from unittest.mock import patch

from core.paper_drug_index import (ALIAS, NAME, NAME_BASE, NAMES_ONLY, OTHER_NAMES, SALT_FORM,
                                   SHORT_ALIAS, DrugIndex, resolve, stereo_key)


def drug(name, *alternates, gene=None):
    d = {"name": name, "alternate_names": list(alternates)}
    if gene:
        d["human_target_gene"] = gene
    return d


class TestStereoKey(unittest.TestCase):
    def test_sign_kept_wherever_it_stands(self):
        self.assertEqual(stereo_key("(-)-Blebbistatin"), "blebbistatin|minus")
        self.assertEqual(stereo_key("blebbistatin-(-)"), "blebbistatin|minus")
        self.assertEqual(stereo_key("Blebbistatin"), "blebbistatin")

    def test_unicode_minus_and_racemates(self):
        self.assertEqual(stereo_key("(−)-Myrtenal"), "myrtenal|minus")
        self.assertEqual(stereo_key("(±)-Equol"), "equol|rac")
        self.assertEqual(stereo_key("(+/-)-Nerolidol"), "nerolidol|rac")
        self.assertEqual(stereo_key("rac-IBT6A"), "ibt6a|rac")

    def test_letter_descriptors_are_left_to_normalize_key(self):
        # R/S and D/L survive normalize_key as letters, so they need no special handling.
        self.assertEqual(stereo_key("(R)-Propranolol"), "rpropranolol")
        self.assertEqual(stereo_key("L-Serine"), "lserine")

    def test_empty(self):
        self.assertEqual(stereo_key(""), "")
        self.assertEqual(stereo_key(None), "")


class TestIndex(unittest.TestCase):
    def test_base_names_come_from_our_names(self):
        idx = DrugIndex(["Sodium dichloroacetate (DCA)", "Heparin Sodium", "Mps1-IN-6 (Compound 9)"])
        self.assertEqual(idx.lookup("DCA", OTHER_NAMES, False), {"Sodium dichloroacetate (DCA)": NAME_BASE})
        self.assertEqual(idx.lookup("Sodium dichloroacetate", OTHER_NAMES, False),
                         {"Sodium dichloroacetate (DCA)": NAME_BASE})
        self.assertEqual(idx.lookup("Heparin", OTHER_NAMES, False), {"Heparin Sodium": NAME_BASE})
        # a generic part of our own name is never indexed
        self.assertEqual(idx.lookup("Compound 9", OTHER_NAMES, False), {})

    def test_short_and_numeric_aliases_are_not_indexed(self):
        idx = DrugIndex(["Tavilermide", "Imatinib"],
                        aliases=[("D3", "Tavilermide"), ("152459-95-5", "Imatinib"), ("123456", "Imatinib")])
        self.assertEqual(idx.lookup("D3", OTHER_NAMES, False), {})
        self.assertEqual(idx.lookup("123456", OTHER_NAMES, False), {})

    def test_alias_of_a_drug_we_do_not_hold_is_ignored(self):
        idx = DrugIndex(["Imatinib"], aliases=[("Gleevec", "Imatinib"), ("Tasigna", "Nilotinib")])
        self.assertEqual(idx.lookup("Tasigna", OTHER_NAMES, False), {})
        self.assertEqual(idx.lookup("Gleevec", OTHER_NAMES, False), {"Imatinib": ALIAS})

    def test_salt_stripped_lookup_needs_a_salt_word(self):
        idx = DrugIndex(["Imatinib"])
        self.assertEqual(idx.lookup("Imatinib", NAMES_ONLY, True), {})
        self.assertEqual(idx.lookup("Imatinib mesylate", NAMES_ONLY, True), {"Imatinib": NAME})


class TestResolveOrder(unittest.TestCase):
    def test_exact_name(self):
        m = resolve(drug("Imatinib"), DrugIndex(["Imatinib"]))
        self.assertEqual((m.drug, m.via), ("Imatinib", NAME))

    def test_synonym_finds_a_drug_held_under_another_name(self):
        # The reason for the rebuild: a batch ran Quizartinib under its code.
        m = resolve(drug("Quizartinib"), DrugIndex(["AC220"], aliases=[("Quizartinib", "AC220")]))
        self.assertEqual((m.drug, m.via, m.matched_name), ("AC220", ALIAS, "Quizartinib"))

    def test_drug_name_beats_a_synonym(self):
        # A real alias points "Paclitaxel" at Nab-paclitaxel. The drug's own name wins.
        idx = DrugIndex(["Paclitaxel", "Nab-paclitaxel"], aliases=[("Paclitaxel", "Nab-paclitaxel")])
        self.assertEqual(resolve(drug("Paclitaxel"), idx).drug, "Paclitaxel")

    def test_drug_name_beats_a_salt_sharing_its_synonyms(self):
        # Looking names and synonyms up together made 476 such matches ambiguous.
        idx = DrugIndex(["Trametinib", "Trametinib DMSO solvate"],
                        aliases=[("Trametinib", "Trametinib DMSO solvate")])
        m = resolve(drug("Trametinib"), idx)
        self.assertEqual(m.drug, "Trametinib")
        self.assertEqual(m.possible, [])

    def test_several_drugs_give_possible_matches(self):
        idx = DrugIndex(["BGJ-398", "NVP-BGJ398"],
                        aliases=[("Infigratinib", "BGJ-398"), ("Infigratinib", "NVP-BGJ398")])
        m = resolve(drug("Infigratinib"), idx)
        self.assertIsNone(m.drug)
        self.assertEqual(m.possible, ["BGJ-398", "NVP-BGJ398"])

    def test_alternates_cannot_break_a_tie_on_the_primary_name(self):
        # TCEP: the extraction model's alternate named the flame retardant.
        idx = DrugIndex(["TCEP Hydrochloride", "Tris(2-chloroethyl) phosphate"],
                        aliases=[("TCEP-x", "TCEP Hydrochloride"),
                                 ("TCEP-x", "Tris(2-chloroethyl) phosphate")])
        m = resolve(drug("TCEP-x", "Tris(2-chloroethyl) phosphate"), idx)
        self.assertIsNone(m.drug)
        self.assertEqual(len(m.possible), 2)

    def test_primary_synonym_beats_an_exact_name_on_an_alternate(self):
        # M1774: the extraction model gave "Elimusertib" as an alternate; M1774 is tuvusertib.
        idx = DrugIndex(["Tuvusertib", "Elimusertib"], aliases=[("M1774", "Tuvusertib")])
        self.assertEqual(resolve(drug("M1774", "Elimusertib"), idx).drug, "Tuvusertib")

    def test_exact_name_on_any_alternate_beats_a_synonym_on_an_earlier_one(self):
        # DBZ: "Dibenzazepine" is an alias of Iminostilbene; "YO-01027" is our entry for DBZ.
        idx = DrugIndex(["Iminostilbene", "YO-01027"], aliases=[("Dibenzazepine", "Iminostilbene")])
        self.assertEqual(resolve(drug("DBZ", "Dibenzazepine", "YO-01027"), idx).drug, "YO-01027")

    def test_name_as_written_before_the_name_without_its_salt_word(self):
        # "phosphate" is a salt word, so "Uridine triphosphate" without it is "Uridine".
        idx = DrugIndex(["Uridine", "INS316"], aliases=[("Uridine triphosphate", "INS316")])
        self.assertEqual(resolve(drug("UTP", "Uridine triphosphate"), idx).drug, "INS316")

    def test_paper_salt_finds_our_base_name(self):
        m = resolve(drug("Imatinib mesylate"), DrugIndex(["Imatinib"]))
        self.assertEqual((m.drug, m.salt_stripped), ("Imatinib", True))

    def test_paper_base_name_finds_our_salt(self):
        m = resolve(drug("Heparin"), DrugIndex(["Heparin Sodium"]))
        self.assertEqual((m.drug, m.via), ("Heparin Sodium", NAME_BASE))

    def test_nothing_found(self):
        m = resolve(drug("Totally unknown"), DrugIndex(["Imatinib"]))
        self.assertIsNone(m.drug)
        self.assertEqual(m.possible, [])


class TestGenericNames(unittest.TestCase):
    def test_generic_primary_name_stops(self):
        idx = DrugIndex(["Compound 11"])
        self.assertIsNone(resolve(drug("Compound 11"), idx).drug)

    def test_compound_c_is_exempt_and_found_by_synonym(self):
        idx = DrugIndex(["Dorsomorphin"], aliases=[("Compound C", "Dorsomorphin")])
        self.assertEqual(resolve(drug("Compound C"), idx).drug, "Dorsomorphin")

    def test_digit_code_is_a_name_not_a_bare_number(self):
        # "666-15" is the name of a drug we hold; only a bare number like "42" is generic.
        idx = DrugIndex(["666-15", "42"])
        self.assertEqual(resolve(drug("666-15"), idx).drug, "666-15")
        self.assertIsNone(resolve(drug("42"), idx).drug)

    def test_generic_alternate_is_skipped(self):
        # G12Di-Acid: the extraction model gave "Compound 20", an alias of an unrelated drug.
        idx = DrugIndex(["Adagrasib"], aliases=[("Compound 20", "Adagrasib")])
        self.assertIsNone(resolve(drug("G12Di-Acid", "Compound 20"), idx).drug)

    def test_none_and_blank_alternates_are_skipped(self):
        idx = DrugIndex(["None"])
        self.assertIsNone(resolve(drug("Unknown thing", "None", "", None), idx).drug)


class TestStereo(unittest.TestCase):
    def test_plain_name_takes_the_plain_entry(self):
        # Atenolol used to land on (-)-Atenolol: the three entries shared one key.
        idx = DrugIndex(["Atenolol", "(+)-Atenolol", "(-)-Atenolol"])
        self.assertEqual(resolve(drug("Atenolol"), idx).drug, "Atenolol")

    def test_labelled_name_takes_its_own_form(self):
        idx = DrugIndex(["Blebbistatin", "blebbistatin-(-)"])
        self.assertEqual(resolve(drug("(-)-Blebbistatin"), idx).drug, "blebbistatin-(-)")

    def test_signed_aliases_no_longer_collide(self):
        # "MK-801" and "(-)-MK-801" used to share a key, so the alias was dropped as ambiguous.
        idx = DrugIndex(["dizocilpine-(+)", "(-)-Dizocilpine"],
                        aliases=[("MK-801", "dizocilpine-(+)"), ("(-)-MK-801", "(-)-Dizocilpine")])
        self.assertEqual(resolve(drug("MK-801"), idx).drug, "dizocilpine-(+)")

    def test_micro_sign_finds_the_greek_mu_name(self):
        # The paper wrote "4\u00b58c" (micro sign); the database holds "4\u03bc8C".
        self.assertEqual(resolve(drug("4\u00b58c"), DrugIndex(["4\u03bc8C"])).drug, "4\u03bc8C")

    def test_plain_name_does_not_take_a_labelled_only_entry(self):
        # Bicuculline: only (+) and (-) entries exist; the paper does not say which.
        idx = DrugIndex(["(+)-Bicuculline", "(-)-Bicuculline"])
        self.assertIsNone(resolve(drug("Bicuculline"), idx).drug)


class TestShortSynonyms(unittest.TestCase):
    """The last lookup: synonyms under 5 characters, accepted only when our report names the
    paper's target gene. Cases from a Paper Query run."""

    REPORTS = {"Azenosertib": "WEE1 inhibitor. Target: WEE1.",
               "Decamethonium": "CHRNA1, CHRNB1: nicotinic receptor blocker.",
               "RAD51 Inhibitor B02": "None (Unverified). B02 was reported to inhibit RAD51 ...",
               "Amprenavir": "HIV-1 Protease", "D-AP5": "GRIN1, GRIN2A",
               "Bortezomib": "PSMB5"}

    def index(self, names, aliases):
        return DrugIndex(names, aliases, report_text=self.REPORTS.get)

    def test_short_code_matches_when_the_report_names_the_paper_gene(self):
        idx = self.index(["Azenosertib"], [("ZN-C3", "Azenosertib")])
        m = resolve(drug("Zn-c3", gene="WEE1"), idx)
        self.assertEqual((m.drug, m.via, m.matched_name), ("Azenosertib", SHORT_ALIAS, "Zn-c3"))

    def test_unverified_report_text_counts(self):
        idx = self.index(["RAD51 Inhibitor B02"], [("B02", "RAD51 Inhibitor B02")])
        self.assertEqual(resolve(drug("B02", gene="RAD51"), idx).drug, "RAD51 Inhibitor B02")

    def test_paper_label_for_another_compound_is_refused(self):
        # The paper's "C10" is a WDR5 inhibitor; our Decamethonium has the PubChem synonym C10.
        idx = self.index(["Decamethonium"], [("C10", "Decamethonium")])
        m = resolve(drug("C10", gene="WDR5"), idx)
        self.assertIsNone(m.drug)
        self.assertEqual(m.possible, [])

    def test_no_paper_gene_no_short_match(self):
        idx = self.index(["Azenosertib"], [("ZN-C3", "Azenosertib")])
        self.assertIsNone(resolve(drug("Zn-c3"), idx).drug)
        self.assertIsNone(resolve(drug("Zn-c3", gene="None"), idx).drug)

    def test_never_replaces_an_ordinary_match(self):
        # Primary "APV" is Amprenavir's short synonym; the alternate D-AP5 is our drug's name.
        idx = self.index(["Amprenavir", "D-AP5"], [("APV", "Amprenavir")])
        m = resolve(drug("APV", "D-AP5", gene="GRIN1"), idx)
        self.assertEqual((m.drug, m.via), ("D-AP5", NAME))

    def test_gene_must_be_a_whole_word(self):
        idx = self.index(["Azenosertib"], [("ZN-C3", "Azenosertib")])
        self.assertIsNone(resolve(drug("Zn-c3", gene="WEE"), idx).drug)

    def test_only_confirmed_candidates_count(self):
        # "BO2": Bortezomib and B02 share the short key; only B02's report names RAD51.
        idx = self.index(["Bortezomib", "RAD51 Inhibitor B02"],
                         [("B02", "RAD51 Inhibitor B02"), ("BO2", "Bortezomib"), ("BO2", "RAD51 Inhibitor B02")])
        self.assertEqual(resolve(drug("BO2", gene="RAD51"), idx).drug, "RAD51 Inhibitor B02")

    def test_without_report_text_short_synonyms_are_unused(self):
        idx = DrugIndex(["Azenosertib"], [("ZN-C3", "Azenosertib")])
        self.assertIsNone(resolve(drug("Zn-c3", gene="WEE1"), idx).drug)


class TestSaltForm(unittest.TestCase):
    """Several drugs found, all one compound: the plain salt entry is taken. Real structures."""

    NMMA = "NTNWOCRCBQPEKQ-YFKPBYRVSA-N"
    ASA = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    PANT = "GHOKWGTUZJEAQD-ZETCQYMHSA-N"
    CROM = "IMZMKUWMOSJXDT-UHFFFAOYSA-N"
    IDENTITY = {
        "L-NMMA acetate": ("IKPNWIGTWUZCKM-JEDNCBNOSA-N", NMMA,
                           "CC(=O)O.CN=C(N)NCCC[C@@H](C(=O)O)N"),
        "Targinine": (NMMA, NMMA, "CN=C(N)NCCC[C@@H](C(=O)O)N"),
        "Aspirin": (ASA, ASA, "CC(=O)OC1=CC=CC=C1C(=O)O"),
        "Carbasalate Calcium": ("VYMUGTALCSPLDM-UHFFFAOYSA-L", ASA,
                                "CC(=O)OC1=CC=CC=C1C(=O)[O-].CC(=O)OC1=CC=CC=C1C(=O)[O-].C(=O)(N)N.[Ca+2]"),
        "Pantothenic acid": (PANT, PANT, "CC(C)(CO)[C@H](C(=O)NCCC(=O)O)O"),
        "D-Pantothenate Sodium": ("GQTHJBOWLPZUOI-FJXQXJEOSA-M", PANT,
                                  "CC(C)(CO)[C@H](C(=O)NCCC(=O)[O-])O.[Na+]"),
        "Calcium D-Panthotenate": ("FAPWYRCQGJNNSJ-UBKPKTQASA-L", PANT,
                                   "CC(C)(CO)[C@H](C(=O)NCCC(=O)[O-])O.CC(C)(CO)[C@H](C(=O)NCCC(=O)[O-])O.[Ca+2]"),
        # Here the salt's name carries the acid's structure, and the reverse.
        "Disodium Cromoglycate": (CROM, CROM, "OC(=O)c1cc(=O)c2c(OCC(O)COc3cccc4oc(C(=O)O)cc(=O)c34)cccc2o1"),
        "cromoglicic-acid": ("IMZMKUWMOSJXDT-UHFFFAOYSA-L", CROM,
                             "[O-]C(=O)c1cc(=O)c2c(OCC(O)COc3cccc4oc(C([O-])=O)cc(=O)c34)cccc2o1.[Na+].[Na+]"),
    }

    def index(self, names, aliases):
        return DrugIndex(names, aliases, identity=self.IDENTITY)

    def test_salt_entry_is_taken_for_one_compound(self):
        idx = self.index(["L-NMMA acetate", "Targinine"],
                         [("L-NMMA", "L-NMMA acetate"), ("L-NMMA", "Targinine")])
        m = resolve(drug("L-NMMA"), idx)
        self.assertEqual((m.drug, m.via, m.matched_name), ("L-NMMA acetate", SALT_FORM, "L-NMMA"))
        self.assertEqual(m.possible, [])

    def test_combination_product_is_not_taken_for_its_parent(self):
        # Carbasalate Calcium is aspirin + urea + calcium: not a plain salt of aspirin.
        idx = self.index(["Aspirin", "Carbasalate Calcium"],
                         [("Acetylsalicylic acid", "Aspirin"), ("Acetylsalicylic acid", "Carbasalate Calcium")])
        m = resolve(drug("Acetylsalicylic acid"), idx)
        self.assertIsNone(m.drug)
        self.assertEqual(m.possible, ["Aspirin", "Carbasalate Calcium"])

    def test_two_salt_entries_stay_possible(self):
        idx = self.index(["Pantothenic acid", "D-Pantothenate Sodium", "Calcium D-Panthotenate"],
                         [("Vitamin B5", d) for d in ("Pantothenic acid", "D-Pantothenate Sodium",
                                                      "Calcium D-Panthotenate")])
        self.assertEqual(len(resolve(drug("Vitamin B5"), idx).possible), 3)

    def test_name_and_structure_must_agree(self):
        # The salt-named entry has the acid's structure: no pick.
        idx = self.index(["Disodium Cromoglycate", "cromoglicic-acid"],
                         [("Cromolyn", "Disodium Cromoglycate"), ("Cromolyn", "cromoglicic-acid")])
        self.assertIsNone(resolve(drug("Cromolyn"), idx).drug)

    def test_different_compounds_stay_possible(self):
        idx = self.index(["L-NMMA acetate", "Aspirin"],
                         [("XYZ-12345", "L-NMMA acetate"), ("XYZ-12345", "Aspirin")])
        self.assertEqual(resolve(drug("XYZ-12345"), idx).possible, ["Aspirin", "L-NMMA acetate"])

    def test_own_name_still_wins(self):
        idx = self.index(["L-NMMA acetate", "Targinine"],
                         [("L-NMMA", "L-NMMA acetate"), ("L-NMMA", "Targinine")])
        self.assertEqual(resolve(drug("Targinine"), idx).via, NAME)

    def test_without_identity_several_drugs_stay_possible(self):
        idx = DrugIndex(["L-NMMA acetate", "Targinine"],
                        [("L-NMMA", "L-NMMA acetate"), ("L-NMMA", "Targinine")])
        self.assertEqual(resolve(drug("L-NMMA"), idx).possible, ["L-NMMA acetate", "Targinine"])


class TestEngineResult(unittest.TestCase):
    """What _verify_drug records from the match."""

    def setUp(self):
        from core.paper_query_engine import PaperQueryEngine
        self.engine = PaperQueryEngine()

    @patch("core.paper_query_engine.get_analysis_fuzzy", return_value=None)
    def test_match_records_how(self, _fuzzy):
        idx = DrugIndex(["AC220"], aliases=[("Quizartinib", "AC220")])
        r = self.engine._verify_drug(drug("Quizartinib"), idx)
        self.assertEqual((r["db_match"], r["match_via"], r["matched_name"]),
                         ("AC220", ALIAS, "Quizartinib"))

    def test_possible_matches_stay_not_in_database(self):
        idx = DrugIndex(["BGJ-398", "NVP-BGJ398"],
                        aliases=[("Infigratinib", "BGJ-398"), ("Infigratinib", "NVP-BGJ398")])
        r = self.engine._verify_drug(drug("Infigratinib"), idx)
        self.assertEqual(r["verdict_text"], "Not in Database")
        self.assertIsNone(r["db_link"])
        self.assertEqual(r["possible_matches"], ["BGJ-398", "NVP-BGJ398"])

    def test_no_match_has_no_possible_matches(self):
        r = self.engine._verify_drug(drug("Totally unknown"), DrugIndex([]))
        self.assertNotIn("possible_matches", r)
        self.assertNotIn("match_via", r)


if __name__ == "__main__":
    unittest.main()
