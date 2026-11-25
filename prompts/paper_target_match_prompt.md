# Target Match Verification (paper matching)

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
6. **Adjacent step in the same pathway**: The paper names a protein upstream or downstream of the DB target in the same signaling chain, directly or through one intermediate step (for example, the kinase that phosphorylates the DB target's regulator), or the cellular process the DB target directly carries out, and the drug's effect on that step runs through the DB target (e.g., paper says "AKT signaling" and the DB target is PI3K). This still counts as a match when the paper loosely calls that adjacent protein the drug's target, for example by saying the drug "inhibits" or "targets" it, while the drug actually acts on the DB target next to it (e.g., the paper calls a PI3K inhibitor "an AKT inhibitor"). A protein reached only through a general cell stress state (such as ER stress, oxidative stress or the DNA damage response) is not in the same signaling chain.
7. **Class description of the drug's profile**: The paper describes the drug by a broad class that fits the drug's overall profile as the PI report describes it (e.g., "multi-kinase inhibitor" or "broad-spectrum tyrosine kinase inhibitor" for a drug the report describes as acting on many kinases). This is a match even when the class fits the drug's alternate targets better than its DB targets. A single named protein is not a class description.

A paper target **does NOT match** (verdict: `"unverified"`) if:

1. **Different target entirely**: The paper claims the drug hits a fundamentally different protein/pathway than what the DB records (e.g., paper says "COX-2 / PTGS2" but DB says "IDO1").
2. **Vague overlap only**: Superficial textual similarity without genuine biological equivalence (e.g., both mention "kinase" but for completely different kinases).
3. **No direct link**: The paper attributes the drug's effect to a protein that is not in the DB target's signaling chain within one intermediate step (see rule 6).
4. **Alternate target only**: The paper's claimed target is one of the drug's alternate (secondary) targets in the PI report. See below.

## Alternate Targets Do Not Match

If the paper's claimed target appears only under **alternate targets** in the PI report (it is not the DB target, not a synonym or family member of it, and not an adjacent step under rule 6), mark the drug `"unverified"`, even when the PI report gives evidence for the drug acting on that alternate target. A paper that uses the drug for a secondary target is not using it for its verified target. The one exception is rule 7: a broad class description of the drug's overall profile. This applies only when the paper's claimed target is listed under Alternate Targets in the PI report. It does not override rules 1–7: an adjacent step in the same pathway (rule 6), or the substrate or structure the DB target acts on, is still a match.

## Important Constraints

- Base your judgment ONLY on the information provided in each example's fields (paper target, paper gene, DB target, DB genes, PI report excerpt). Do NOT rely on external pharmacological knowledge about what the drug "really" targets. You may use general knowledge of how proteins and pathways relate (for example, that a gene symbol names a protein next to the DB target) to apply rules 2–7; just not to decide what the drug targets.
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
