import unittest
from core.utils import is_duplicate

class TestDuplicationLogic(unittest.TestCase):
    
    def test_same_title_different_years_wide_gap(self):
        """
        Titles match exactly, but years are 2021 vs 2018. 
        Should NOT be a duplicate (return False).
        """
        existing_paper = {
            "title": "Target Identification of Small Molecules Using Large-Scale CRISPR-Cas Mutagenesis Scanning of Essential Genes",
            "year": "2021",
            "doi": "10.1007/protocol",
            "url": "http://protocol"
        }
        
        new_paper = {
            "title": "Target Identification of Small Molecules Using Large-Scale CRISPR-Cas Mutagenesis Scanning of Essential Genes",
            "year": "2018",
            "doi": "10.1038/nature",
            "url": "http://nature"
        }
        
        # Currently this returns True (Duplicate) because it ignores year
        # We want it to be False
        is_dup = is_duplicate(new_paper, [existing_paper])
        print(f"Is Duplicate (2021 vs 2018): {is_dup}")
        self.assertFalse(is_dup, "Should not be duplicate if years differ significantly (2021 vs 2018)")

    def test_same_title_diff_years_small_gap(self):
        """
        Titles match exactly, years are 2018 vs 2019.
        Should BE a duplicate (return True) due to potential publication lag usage.
        """
        existing_paper = {
            "title": "Novel Drug Discovery",
            "year": "2018"
        }
        
        new_paper = {
            "title": "Novel Drug Discovery",
            "year": "2019"
        }
        
        is_dup = is_duplicate(new_paper, [existing_paper])
        print(f"Is Duplicate (2018 vs 2019): {is_dup}")
        self.assertTrue(is_dup, "Should be duplicate if years are close (<= 1 year)")

    def test_same_title_invalid_years(self):
        """
        matched title, but one year is missing. Default to duplicate to be safe?
        Or strict? 
        If years are missing, we fall back to title match -> True.
        """
        p1 = {"title": "Paper A", "year": "2020"}
        p2 = {"title": "Paper A", "year": ""}
        
        self.assertTrue(is_duplicate(p2, [p1]))

if __name__ == '__main__':
    unittest.main()
