import os
import logging
from typing import List, Dict

# Core imports
from .db import (
    get_analysis_fuzzy,
    normalize_key,
)
from .paper_drug_index import DrugIndex, resolve
from .target_matching import PAPER_TARGET_MATCH_PROMPT, llm_check_target_match

# Logging
logger = logging.getLogger(__name__)

ALWAYS_NON_TARGETED = {
    normalize_key(d) for d in [
        "doxycycline", "Lipopolysaccharide", "LPS", "dTAG-v1", "dTAG-13",
        "puromycin", "geneticin", "G418", "blasticidin", "hygromycin",
        "hygromycin B", "MG-132", "MG132", "Oligomycin", "insulin", "zeocin",
        "cycloheximide", "nocodazole", "Isoflurane", "Testosterone", "estrogen",
        "Ethidium bromide", "taxol", "colcemid", "Penicillin", "Streptomycin",
        "polybrene", "tamoxifen", "4-hydroxytamoxifen", "Diphtheria toxin",
        "Nab-paclitaxel", "Sodium butyrate",
        "Poly(I:C)", "Poly(dA:dT)", "hCG", "Human chorionic gonadotropin",
        "IFN-beta", "IFN-alpha", "IFN-gamma",
        "Dihydrocytochalasin B", "Cytochalasin B", "Cytochalasin", "DCB",
        "Bortezomib", "Rotenone", "Antimycin", "Antimycin A", "FCCP",
        "Neomycin", "Luciferin", "D-luciferin",
        "Leupeptin", "Chloroquine", "Actinomycin D", "Actinomycin",
        "Biotin", "Epoxomicin", "Bafilomycin", "Bafilomycin A1",
        "Shield-1", "Colchicine",
        "Indole-3-acetic acid", "1-nm-pp1", "1nm-pp1",
        "2-deoxy-d-glucose", "Aspartate", "Glutamate", "Glycine",
        "Glucose", "Glutamine", "IL-27",
        "AP1903",
        "Z-VAD-FMK", "ZVAD-FMK", "ZVAD", "Q-VD-Oph",
        "Phorbol myristate acetate", "Phorbol 12-myristate 13-acetate",
        "12-myristate 13-acetate", "PMA", "Hydroxyurea",
        "AP20187", "AP-20187", "Auxin", "AP-21967", "AP21967",
        "Vitamin D3", "Vitamin B6", "Vitamin B12",
        "TNFα", "TNF-alpha", "EGF",
        "Atezolizumab", "Camrelizumab", "Sintilimab",
        "Tislelizumab", "Toripalimab", "Sugemalimab", "Trastuzumab",
        "Y-27632", "Y27632",
        "MTA", "Methylthioadenosine",
        "2'3'-cGAMP", "cGAMP",
        "1,6-hexanediol", "N-acetylcysteine", "N-acetyl-L-cysteine",
        "N-acetyl cysteine", "Sodium azide",
        # Used in papers to block clathrin-mediated endocytosis; our report verifies it as a D2
        # antagonist and lists that use only as an alternate target.
        "Chlorpromazine", "Chlorpromazine hydrochloride",
        # Used in papers as a metabolic / AMPK tool at high doses; our report is UNVERIFIED.
        "Metformin", "Metformin hydrochloride", "Metformin HCl",
        "Hydroxychloroquine", "Hydroxychloroquine sulfate", "Hydroxychloroquine sulphate", "HCQ",
        "Genistein",
        "Nystatin",
        "Copper(II) sulfate", "Copper(II) sulphate", "Copper sulfate", "Copper sulphate",
        "Cupric sulfate", "CuSO4",
        "Chondroitin sulfate", "Chondroitin sulphate", "Chondroitin sulfate sodium",
        "Heparin", "Heparin sodium",
    ]
}


class PaperQueryEngine:
    """
    Paper Query's extraction prompt, and the match and target check of one compound extracted
    from a paper. scripts/batch_paper_pipeline.py sends the papers to Gemini and calls these.
    """

    def __init__(self):
        # Load prompt
        prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "paper_query_prompt.md")
        try:
            with open(prompt_path, "r") as f:
                self.prompt_template = f.read()
        except FileNotFoundError:
            logger.error(f"Prompt file not found at {prompt_path}")
            self.prompt_template = "Extract every drug, its target, and context from this paper. Return JSON."

    def _verify_drug(self, drug_data: Dict, index: DrugIndex) -> Dict:
        """
        Verifies a single drug against the database to determine status.

        Args:
            drug_data: The drug item from the paper analysis.
            index: core.paper_drug_index.DrugIndex, built once per paper (or batch run).

        The match itself is core.paper_drug_index.resolve: one drug found -> match; several ->
        no match, with the candidates in `possible_matches`; none -> Not in Database.
        """
        extracted_name = drug_data.get("name", "Unknown")
        # Handle both raw input ('target') and re-verification input ('paper_target')
        # Prioritize 'paper_target' if it exists (from previous verification) to avoid overwriting or losing data
        extracted_target = drug_data.get("paper_target") or drug_data.get("target", "None")
        extracted_gene = drug_data.get("human_target_gene", "None")
        
        result = {
            "name": extracted_name,
            "paper_target": extracted_target,
            "human_target_gene": extracted_gene,
            "context": drug_data.get("context", ""),
            "alternate_names": drug_data.get("alternate_names", []),
            "verdict_icon": "⚪", # Default Gray for Not in DB
            "verdict_text": "Not in Database",
            "selectivity": "Unknown",
            "db_link": None
        }
        
        matched_db_name = None
        analysis = None

        match = resolve(drug_data, index)
        if match.possible:
            # Several database drugs fit the paper's name. No person reviews the match, so
            # none is chosen; the candidates are shown instead.
            result["possible_matches"] = match.possible
        if match.drug:
            matched_db_name = match.drug
            result["match_via"] = match.via
            result["matched_name"] = match.matched_name
            analysis = get_analysis_fuzzy(matched_db_name)

        if matched_db_name:
            result["db_match"] = matched_db_name
            result["db_link"] = matched_db_name 
            
            if analysis:
                summary = analysis.get("structured_summary", {})
                
                # Extract fields
                db_verdict = str(summary.get("final_verdict", "Unknown")).upper()
                db_selectivity = str(summary.get("selectivity_grade", "Unknown")).upper()
                db_target = str(summary.get("target", ""))
                db_targets_list = summary.get("target_gene_names", [])
                
                # Display value: a stored null grade is blank, not "None".
                result["selectivity"] = str(summary.get("selectivity_grade", "Unknown") or "").title()
                result["db_target_genes"] = db_targets_list  # For alternate compounds
                
                # Logic for Classification (Refined)
                # Green: Verified + Match + (Selective OR Moderately Selective)
                # Yellow: Verified + Match + (Promiscuous OR Unknown)
                # Red: Not Verified OR Target Mismatch
                
                is_verified = db_verdict == "VERIFIED"

                # Target Match Logic
                # Check target string AND human gene
                is_target_match = self._check_target_match(
                    extracted_target, extracted_gene, db_target, db_targets_list,
                    extracted_name, matched_db_name, db_selectivity,
                    analysis.get("report", "")
                )

                # LLM call failed — mark as failed, not verified/unverified
                if is_target_match is None:
                    result["verdict_icon"] = "⚠️"
                    result["verdict_text"] = "LLM Failed"
                    return result

                # Selectivity Logic
                # We normalize strictly to check against known statuses
                clean_selectivity = db_selectivity.upper()
                is_good_selectivity = clean_selectivity in ["SELECTIVE", "MODERATELY SELECTIVE"]

                if not is_verified:
                        result["verdict_icon"] = "🔴"
                        result["verdict_text"] = "Unverified"

                elif not is_target_match:
                        # Even if verified for target A, if paper says B, it's a red flag (context mismatch)
                        result["verdict_icon"] = "🔴"
                        # Only Verified, Unverified and Unknown are used: a mismatch makes the paper's claim Unverified.
                        result["verdict_text"] = "Unverified"

                else:
                    # Verified AND Match
                    result["verdict_text"] = "Verified"

                    if is_good_selectivity:
                            result["verdict_icon"] = "🟢"
                    else:
                            result["verdict_icon"] = "🟡" 
                         
        return result

    def _check_target_match(self, extracted_str: str, extracted_gene: str,
                            db_target_str: str, db_target_list: List[str],
                            drug_name: str = "", db_drug_name: str = "",
                            db_selectivity: str = "", pi_report: str = "") -> bool | None:
        """Thin wrapper — delegates to LLM target matching with the paper prompt. Returns None on failure."""
        return llm_check_target_match(
            extracted_str, extracted_gene, db_target_str, db_target_list,
            drug_name, db_drug_name, db_selectivity, pi_report,
            prompt=PAPER_TARGET_MATCH_PROMPT,
        )
