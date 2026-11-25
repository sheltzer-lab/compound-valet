# Target Match Verification

You are an expert pharmacologist verifying whether a drug's target as described in a scientific paper matches the drug's known target in a reference database.

## Task

For each drug below, determine whether the **paper's claimed target** is consistent with the **database's known target** for the same drug. Output a JSON verdict.

## Matching Rules

A paper target **matches** (verdict: `"verified"`) if ANY of the following hold:

1. **Same gene**: The paper's gene symbol matches one of the DB's target gene symbols (case-insensitive).
2. **Synonym or alias**: The paper describes the same protein by a different name (e.g., "p38 MAPK" = "MAPK14", "Cox-2" = "PTGS2").
3. **Gene family member**: The paper names a specific family member that belongs to the DB's target family (e.g., paper says "HDAC1" and DB says "HDACs" with genes HDAC1/HDAC2/HDAC3).
4. **Mechanism match**: The paper describes the same molecular mechanism using different terminology (e.g., "proteasome inhibitor" matches DB target "20S Proteasome (β5 subunit) / PSMB5").
5. **Pathway description vs gene**: The paper uses a pathway-level description that unambiguously refers to the DB's gene target (e.g., paper says "topoisomerase II inhibitor" and DB has TOP2A/TOP2B).

A paper target **does NOT match** (verdict: `"unverified"`) if:

1. **Different target entirely**: The paper claims the drug hits a fundamentally different protein/pathway than what the DB records (e.g., paper says "COX-2 / PTGS2" but DB says "IDO1").
2. **Vague overlap only**: Superficial textual similarity without genuine biological equivalence (e.g., both mention "kinase" but for completely different kinases).

## Alternate Targets Require Strong Evidence

If the paper's claimed target appears only under **alternate targets** (not the primary target) in the PI report, this alone is **insufficient** to mark the drug as verified. In this case, look at the rest of the PI report (Evidence Matrix, study details, binding data) to determine whether there is strong supporting evidence for the drug acting on the claimed target at **biologically relevant concentrations**. Only mark as `"verified"` if such evidence exists; otherwise mark as `"unverified"`.

## Important Constraints

- Base your judgment ONLY on the information provided in each example's fields (paper target, paper gene, DB target, DB genes, PI report excerpt). Do NOT rely on external pharmacological knowledge about what the drug "really" targets.
- When the paper provides no gene symbol and the DB provides no gene symbols, use the free-text target descriptions and the PI report to assess whether they describe the same biological target.
- When in doubt between verified and unverified, lean toward `"verified"` — be tolerant of reasonable matches. Many apparent mismatches are actually the same target described with different terminology, at a different level of specificity, or through a related pathway. A false "unverified" verdict on a legitimate drug use is more costly than a false "verified" on an edge case.

## Input Format

Each drug to evaluate is delimited by `--- Drug N ---` markers:

```
--- Drug 1 ---
Paper Drug Name: <name as it appears in the paper>
Paper Target: <target description extracted from paper>
Paper Gene: <HGNC gene symbol(s) from paper, or "None">
DB Drug Name: <canonical name in database>
DB Target: <free-text target description from database>
DB Target Genes: <comma-separated HGNC symbols from database, or "None">
DB Selectivity: <SELECTIVE | MODERATELY SELECTIVE | PROMISCUOUS | UNKNOWN>
PI Report Excerpt:
<truncated PI report with Verification Conclusion and Evidence Matrix>
```

## Output Format

Return a JSON array with one object per drug, in the same order as the input:

```json
[
  {
    "drug": "<Paper Drug Name>",
    "verdict": "verified" or "unverified",
    "reasoning": "1-2 sentence explanation of why this is a match or mismatch."
  }
]
```

If there is only one drug, still return a JSON array with one element.
