You are curating the list of search aliases for ONE confirmed chemical compound. The
identity is already settled (below). You are given candidate synonyms aggregated from
PubChem across the compound's salt-union (the parent CID plus all CIDs that share that
parent). PubChem synonym lists are noisy: a depositor may attach a name that actually
belongs to a **different molecule**, and salt-union can pull in a stray alias. Your job
is to keep only the names that genuinely identify **this** compound, and drop the rest.

Additionally, keep only the **most useful** synonyms: return **at most the top 30**, ordered
most-useful first. Some aliases are correct but so seldom used they're just noise — exclude those.
Prefer the names a scientist or a database would actually use: the generic/INN name, the major
brand name(s), the principal research/development code, and the key registry IDs (CAS, ChEMBL, UNII,
DrugBank, ChEBI).

## The confirmed compound
- Preferred name: {preferred_name}
- Molecular formula: {formula}
- SMILES: {smiles}
- PubChem CID: {cid}
- What the report says it is: {target}

## Candidate synonyms (one per line, indexed)
{synonyms}

## Rules
- KEEP: generic/INN names, trade/brand names, research/development codes, registry
  identifiers (CAS, UNII, ChEMBL/DrugBank/ChEBI IDs), common abbreviations, and correct
  systematic (IUPAC) names — provided they refer to THIS molecule (this exact structure,
  or a salt/hydrate/free-base of it).
- DROP: any name that names a **different** compound (a misidentification — e.g. an
  unrelated drug whose name collided via a shared depositor synonym), obvious junk
  (vendor catalog line-noise, screening-plate IDs unless they're the canonical research
  code), and overly generic placeholders ("compound 5", "inhibitor", "test article").
- DROP: uncommon seldom used identifiers.
- When you are not confident a synonym belongs to this exact molecule, **drop it.**
  Precision matters far more than recall here: a wrong alias causes a misidentification
  downstream, a missing alias only costs a little recall.
- Stereochemistry and salt form: a racemate vs. a single enantiomer are DIFFERENT — drop
  a synonym if it denotes a different stereochemistry. A salt/hydrate/free-base of the
  same moiety is the SAME — keep it.

## Output — STRICT JSON, nothing else
A JSON array of **at most 30** synonym strings — the most useful identifiers for this compound,
**ordered most-useful first** — each copied **verbatim** from the candidate list above (do not
invent, reword, normalize, or merge). Example:
["Imatinib", "Gleevec", "STI571", "imatinib mesylate", "152459-95-5"]

Return only strings that appear in the candidate list. Return [] if none qualify.
