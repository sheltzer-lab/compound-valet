# Glossary

**Compound.** A small molecule, named as the user gives it (for example "Imatinib"). The database has one report per compound name. The code often calls it a "drug".

**Run.** One pass of the pipeline over one compound ([pipeline.md](pipeline.md)). It makes about a dozen Gemini calls.

**Report.** What a run produces: the PI's final report, the specialist reports, the reviews, and the structured fields below. Saved in `analysis_results` ([database.md](database.md)).

**Verdict.** VERIFIED: the published evidence supports the compound's action on the named target. UNVERIFIED: it does not, or not yet. UNVERIFIED does not mean "disproven"; it often means the evidence is not enough. The bar is higher for targets that are hard to drug (`prompts/pi_persona.md`).

**Target, target genes.** The target as text (for example "BRD4 (BD1)") and its human HGNC gene symbols (for example `BRD4`). Different fields; do not use one for the other.

**Selectivity grade.** How specific the compound is for its target. The full rules are in `prompts/pi_persona.md`; in short:
- SELECTIVE: spares the large majority of a panel of 10 or more off-targets, tested together in one assay in one paper, and is at least 30-fold more potent on its target than on its strongest off-target.
- MODERATELY SELECTIVE: hits a small minority of a panel of 3 or more off-targets, with a smaller panel or a 5–30-fold window. Typical of compounds that hit a few related targets.
- PROMISCUOUS: no meaningful discrimination, or convincing evidence that it hits several unrelated targets at about the same potency.
- UNKNOWN: no qualifying panel and no evidence of promiscuity.
- Left out: the grade does not apply (for example a DNA-damaging agent with no protein target).

The PI prompt allows an UNVERIFIED compound only PROMISCUOUS or UNKNOWN. The code does not enforce this, so check it when you read the database.

**Mechanism class.** Inhibitor, Activator, Modulator, Degrader, Multiple mechanisms, Other, or Unknown. Unknown for every UNVERIFIED compound.

**Specialist.** One of three models that read the verified papers from one point of view: genetics, biochemistry, biophysics.

**PI.** The "principal investigator" model. It weighs the specialist reports, writes the draft report, and revises it after the reviews.

**Reviewer.** One of three models that critique the PI's draft: Reviewer #1 (neutral, exploratory), Reviewer #2 (critic), and the Selectivity Reviewer.

**Search angle.** One of the three literature searches at the start of a run: Mechanism, Genetics, Clinical.

**Grounding.** Gemini's Google Search tool. The searches, specialists, PI draft and revision, and reviewers use it; identity resolution does not.

**Citation check.** The check every paper must pass before a model may cite it: Crossref or Semantic Scholar must have a matching record, and the link must be safe ([pipeline.md](pipeline.md), stage A).

**Verified paper.** A paper that passed the citation check. Reports cite it by ID, `<compound>-Paper-N`, which becomes an `[Author, Year](link)` link at the end of the run.

**Identity.** The PubChem structure a compound name refers to: InChIKey, parent InChIKey (the structure without its salt), SMILES, and registry IDs (CAS, ChEMBL, DrugBank and others). Found by identity resolution from the offline PubChem index.

**Consensus.** Running a compound several times and saving the run the runs agree on ([consensus.md](consensus.md)).
