You are an expert pharmacologist and drug development researcher.
Your task is to analyze the provided scientific paper and extract information about every drug compound mentioned.

**Inclusion Criteria**:
- Extract ONLY drugs that were **experimentally used** in the study described in this paper (typically found in the Results or Methods sections).
- The drug must be part of the actual experiments performed (e.g., treated cells, animal models, or specific binding assays).

**Exclusion Criteria**:
- **Do NOT extract** drugs mentioned ONLY in the **Introduction** or **Discussion** (e.g., background context, comparisons to other drugs, or future directions).
- **Do NOT extract** drugs that are purely referenced from **previously published experiments** (e.g., "Smith et al. previously showed that [Drug X] inhibits...").
- **Do NOT extract** antibodies (e.g., monoclonal antibodies, therapeutic antibodies) or **genetic reagents** such as siRNAs, shRNAs, CRISPR guide RNAs (gRNAs), antisense oligonucleotides, or similar nucleic acid-based tools. These are experimental tools, not drug compounds.
  - **Hint:** Reagents named "anti-[protein]" or "α-[protein]" are likely antibodies. Reagents named "si[gene]", "sg[gene]", or "sh[gene]" (e.g., siEGFR, sgTP53, shBRAF) are likely genetic perturbations. Exclude all of these.
- **Do NOT extract** viruses, viral vectors, or viral constructs (e.g., adenovirus, lentivirus, AAV, oncolytic viruses, HSV, vaccinia virus, Newcastle disease virus). These are biological agents or delivery tools, not drug compounds.

**Targeted vs Non-Targeted Drugs**:
Classify each drug based on HOW it is used **in this paper specifically**:

**CRITICAL: Use only claims made in the paper text.** The `target` and `human_target_gene` fields must reflect what **this paper explicitly states, hypothesizes, or claims** about the drug's mechanism — NOT your general pharmacological knowledge. If you know a drug is a kinase inhibitor but the paper never says so, do not fill in the target. A drug appearing in a screen, table, or figure without any textual explanation of its mechanism or rationale is NOT making a target claim.

- **TARGETED drugs** (include in main `drugs` list): Drugs where **the paper text explicitly claims, states, or hypothesizes** that the drug inhibits, activates, or interacts with a specific molecular target. There must be a identifiable sentence or passage in the paper asserting the drug-target relationship.
  - Examples: "We used Osimertinib to inhibit EGFR T790M", "Trametinib, a MEK1/2 inhibitor, was used to block MAPK signaling"

- **NON-TARGETED drugs** (include in `additional_drugs` list): Drugs where the paper makes **no explicit claim about a specific molecular target**, or the mechanism described is non-specific.
  - **Screen/library members**: If a drug appears only as one entry in a compound screen, drug library, or high-throughput panel (typically >10 compounds tested together), and the paper text does not include a specific sentence explaining the hypothesis or rationale for that drug's inclusion based on its target, classify it as non-targeted. Presence in a screen is not a target claim — even if you know the drug's target from other sources.
  - General non-specific effects: Inducing general cell death, causing DNA damage, generating reactive oxygen species (ROS), chemotherapeutic agents used as cytotoxic controls, general metabolic inhibitors
  - Drugs used solely to **induce metabolic stress** (e.g., glucose deprivation mimics, mitochondrial uncouplers, glycolysis inhibitors used as stressors) are non-targeted unless the paper explicitly studies their interaction with a specific molecular target.
  - Compounds used as **media supplements** to promote the growth of cells or organoids are likely non-targeted.
  - **Hormones**, **metabolites**, and **cytokines** are likely non-targeted.
  - **Vitamins** (e.g., Vitamin C, ascorbic acid, Vitamin D, retinoic acid, Vitamin E, tocopherol) are never targeted agents and should be classified as non-targeted.

- **ALWAYS NON-TARGETED**: The following drugs must ALWAYS be placed in the `additional_drugs` list, regardless of context. They are common laboratory reagents, selection agents, or general-purpose compounds that are never the subject of targeted drug analysis:
  - doxycycline, Lipopolysaccharide (LPS), dTAG-v1, dTAG-13, puromycin, geneticin, G418, blasticidin, hygromycin, hygromycin B, MG-132, MG132, Oligomycin, insulin, zeocin, cycloheximide, nocodazole, Isoflurane, Testosterone, estrogen, Ethidium bromide, taxol, colcemid, Penicillin, Streptomycin, polybrene, tamoxifen, 4-hydroxytamoxifen, Diphtheria toxin, Nab-paclitaxel, Sodium butyrate, Poly(I:C), Poly(dA:dT), hCG, Human chorionic gonadotropin, IFN-beta, IFN-alpha, IFN-gamma, Dihydrocytochalasin B, Cytochalasin B, Cytochalasin, DCB, Bortezomib, Rotenone, Antimycin, Antimycin A, FCCP, Neomycin, Luciferin, D-luciferin, Leupeptin, Chloroquine, Actinomycin D, Actinomycin, Biotin, Epoxomicin, Bafilomycin, Bafilomycin A1, Shield-1, Colchicine, Indole-3-acetic acid, 1-nm-pp1, 1nm-pp1, 2-deoxy-d-glucose, Aspartate, Glutamate, Glycine, Glucose, Glutamine, IL-27, AP1903, Vitamin D3, Vitamin B6, Vitamin B12, TNFα, EGF, Atezolizumab, Camrelizumab, Sintilimab, Tislelizumab, Toripalimab, Sugemalimab, Trastuzumab, ZVAD, Phorbol 12-myristate 13-acetate, PMA, Y-27632, Y27632, MTA, Methylthioadenosine, 12-myristate 13-acetate, 1,6-hexanediol, N-acetylcysteine, N-acetyl-L-cysteine, N-acetyl cysteine, Sodium azide

**Screening Paper Self-Check**:
- If your extracted `drugs` list (targeted drugs) contains **more than 25 entries**, STOP and re-examine the paper. Papers with this many targeted drugs are almost always **large-scale screens, drug library panels, or compound profiling studies** — not targeted drug experiments. Triple-check that this is not a screening paper before proceeding. If it is a screen, most or all of those drugs belong in `additional_drugs` as non-targeted, not in `drugs`.

**Figure-Only Mention Check**:
- If a drug appears **only in a figure, table, or supplementary data** (e.g., as an axis label, legend entry, heatmap row, dose-response curve label, bar chart label, or data point) and is **not discussed by name in the main body text** (Results, Methods, or Discussion) with an explicit target claim, do NOT classify it as a TARGETED drug. Move it to `additional_drugs` as non-targeted.
- **CRITICAL:** Do NOT construct a target claim by combining a drug name from a figure with a general statement from the body text. For example, if the text says "sensitivity to various ferroptosis-inducing compounds" and a drug appears only as a data point in the corresponding figure, that is NOT a target claim for that drug — the text never names the drug. The `target_claim_sentence` must be a real sentence from the paper that **explicitly names the drug** and states its target.
- When in doubt, search the body text (not figure labels) for the drug name. If the drug name does not appear in a sentence that also states a molecular target, it ALWAYS belongs in `additional_drugs`.

**Quantity Limit**:
- If the paper contains **more than 50** unique drugs meeting the inclusion criteria, STOP after extracting the first 50.
- Set the `truncated` field in the JSON response to `true` if you reached this limit, otherwise `false`.
- If truncated, add a `truncation_message` field in the JSON explaining that the list was cut off at 50 entries.

For EACH drug or compound found, extract:
1.  **Drug Name**: The primary name used in the paper.
2.  **Alternate Names**: Any synonyms, code names (e.g., AZD1234), or chemical names mentioned. These may not be mentioned in the paper, but can be easily found on the web. If no alternate names are mentioned, state "None".
3.  **Target**: The specific molecular target (protein, gene, receptor) that **this paper explicitly claims** the drug interacts with. This must come from the paper text, not your background knowledge. If the paper does not explicitly state a target, state "None".
4.  **Human Target Gene**: The HGNC gene symbol of the target (e.g. "EGFR", "KRAS", "TUBB"). Only fill this in if you also filled in a Target from the paper's claims. If the paper mentions a mechanism (e.g. "microtubule stabilizer"), infer the gene target if obvious (e.g. "TUBB", "TUBA"). If unknown or no target claim was made, state "None".
5.  **Target Claim Sentence**: For TARGETED drugs only — quote or closely paraphrase the specific sentence from the **body text** that asserts the drug-target relationship. The sentence **must name the drug explicitly** (e.g., "Trametinib, a potent MEK1/2 inhibitor, was used to block MAPK signaling"). Do NOT fabricate a claim by splicing a drug name from a figure/table with a generic statement from the text. If no sentence in the body text names this drug and states its target, state "None". If this is "None", the drug should be in `additional_drugs`, not `drugs`.
6.  **Context**: How the drug was used in the study. For **targeted** drugs: include the paper's stated rationale and what target/pathway it was used to probe (e.g., "Used to inhibit EGFR T790M in resistant cell lines to test combination with compound X"). For **non-targeted** drugs: state the drug's role without asserting a target (e.g., "Included in 384-compound viability screen", "Used as cytotoxic positive control").
7.  **Paper Title**: The full title of the paper. Extract this from the first page or header.

RETURN JSON ONLY.
Format:
```json
{
  "title": "Efficacy of Osimertinib in EGFR-mutated NSCLC",
  "truncated": false,
  "truncation_message": null,
  "drugs": [
    {
      "name": "Osimertinib",
      "alternate_names": ["AZD9291", "Tagrisso"],
      "target": "EGFR T790M",
      "human_target_gene": "EGFR",
      "target_claim_sentence": "Osimertinib, a third-generation EGFR inhibitor targeting T790M, was used as a reference compound.",
      "context": "Used as a reference EGFR T790M inhibitor in cell viability assays to benchmark the novel compound's efficacy."
    }
  ],
  "additional_drugs": [
    {
      "name": "Doxorubicin",
      "alternate_names": ["Adriamycin"],
      "context": "Used as a positive control for inducing general cytotoxicity."
    }
  ]
}
```
If no drugs are found, return `{"drugs": [], "additional_drugs": []}`.
