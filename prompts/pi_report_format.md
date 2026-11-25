# Output Format (Markdown)

## 1. Verification Conclusion

**Verdict**: [VERIFIED / UNVERIFIED]

**Target(s)**: [Target Name. **Binding-site tag**: a) if the target protein has more than one distinct functional domain, explicitly tag which functional domain the compound engages - e.g. "BRD4 (BD1)" vs "BRD4 (BD2)". If the compound engages more than one domain of the same protein, state that too - e.g. "BRD4 (BD1 and BD2)". b) Most inhibitors are orthosteric, that is bind directly in the functional domain. If a compound is orthosteric, that does not need to be tagged. However, some compounds are allosteric, that is bind at a site distinct from the active site. If a compound has been conclusively demonstrated in the data to be allosteric, tag it as such - e.g. "MEK1 (allosteric)". For most compounds, no discussion of binding-site is warranted.]

**Target gene name(s)**: [List of HGNC Gene Names, e.g. "EGFR", "MYC". Gene names ONLY, needs to be machine readable. Verify against HGNC database. Can be multiple if not selective. Leave blank if not applicable (e.g. DNA damaging agent). If blank, do not include this line in the output.]

**Selectivity**: [SELECTIVE / MODERATELY SELECTIVE / PROMISCUOUS / UNKNOWN] (Rationale: e.g. "30-fold selectivity for primary target over next-best off-target") *Include only if relevant*

**Alternate Targets**: [List any other reported targets found in literature, distinct from the primary target. Optional. If alternate targets is blank, do not include it in the output schema.]

**Key Rationale**: [2-3 sentences. If druggability is low, explain why evidence was/was not sufficient to overcome the high bar.]


## 2. Evidence Matrix
*Double check your grading: Did you mark Genetics as "Strong" without resistance mutations? If so, CORRECT IT to "Moderate".*
| Evidence Type | Finding | Support Level (Strong/Weak/None) | Source |
| :--- | :--- | :--- | :--- |
| **Genetics** | [e.g. KO phenotype mimics drug] | Moderate | [Citation] |
| **Biochemistry** | [e.g. Kd = 1.2nM] | Strong | [Citation] |
| **Biophysics** | [e.g. Crystal Structure 2HYY] | Strong | [Citation] |
| **Clinical** | [e.g. Phase 3 success] | Moderate | [Citation] |

## 3. Detailed Synthesis
**Target Druggability**: [High / Medium / Low] (Rationale: e.g. "Transcription factor with no defined pocket")

**Mechanism of Action**: [Write a cohesive narrative explaining the mechanism. Discuss discrepancies. If Genetics says "Target A" but Biochemistry says "Target B", resolve the conflict.]

**Selectivity Rationale**: [Explain why the specific selectivity score was chosen. Mention if the off-target panel was sufficiently broad or if data was limited.]

## 4. References
*   [List of papers cited above]

# Structured Output
Finally, after the report, output a **single JSON object** containing these metadata fields. Do not include this in the markdown rendering, but append it at the very end in a code block.

> The `mechanism` (mechanism class) label belongs **here only**. It must NOT appear as a "Mechanism Class" line, field, or heading in the markdown report above (sections 1–4) — the prose gets the *Mechanism of Action* narrative; the class label is read from this JSON.

```json
{
  "final_verdict": "VERIFIED / UNVERIFIED",
  "target": "string",
  "target_gene_names": ["string (HGNC) (Optional — omit if blank)"],
  "alternate_targets": ["string (Optional — omit if blank)"],
  "binding_site": "string (Optional) — the specific site/domain engaged, matching the tag used in **Target(s)** above. Two cases: (a) target has multiple functional domains → name the domain(s) engaged, e.g. 'BD1', 'BD2', or 'BD1 and BD2' if it engages both; (b) compound is conclusively allosteric → 'allosteric'. Omit for orthosteric binders, single-domain targets, or unknown site.",
  "key_evidence_type": "string",
  "druggability": "High / Medium / Low",
  "mechanism": "Inhibitor / Activator / Modulator / Degrader / Multiple mechanisms / Other / Unknown",
  "selectivity_grade": "SELECTIVE / MODERATELY SELECTIVE / PROMISCUOUS / UNKNOWN / null",
  "selectivity_fold": "number or null",
  "primary_off_target": "string or null",
  "key_sources": ["string (Author, Year)"]
}
```
