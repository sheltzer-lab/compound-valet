"""Shared SMILES canonicalizer, for drug_synonyms.canonicalized_smiles.

Every stored structure goes through this one piece of logic, so structures are standardized
the same way. It stays in SMILES space end to end (no InChIKey).
"""


def _mol_from_smiles(smiles):
    """Parse smiles with RDKit; returns an RDKit Mol, or None if invalid/unparseable."""
    if not smiles or not smiles.strip():
        return None
    try:
        from rdkit import Chem
        from rdkit import RDLogger
        RDLogger.DisableLog("rdApp.*")
    except ImportError:
        return None
    try:
        return Chem.MolFromSmiles(smiles.strip())
    except Exception:
        return None


def canonicalize(smiles):
    """Canonicalize a SMILES to its standard as-drawn form (salts/stereo preserved).

    Returns the canonical SMILES string, or None if `smiles` is empty or not a valid molecule.
    """
    mol = _mol_from_smiles(smiles)
    if mol is None:
        return None
    from rdkit import Chem
    return Chem.MolToSmiles(mol)


def canonicalize_parent(smiles):
    """Canonicalize a SMILES after stripping salts/solvents down to the largest organic fragment,
    and neutralizing formal charge (e.g. a protonated base canonicalizes the same as its neutral
    free-base form — they're the same compound at a different point on a pH equilibrium, not two
    different compounds the way a salt pairing is).

    Returns the canonical, salt-stripped, charge-neutral SMILES string, or None if `smiles` is
    empty or not a valid molecule.
    """
    mol = _mol_from_smiles(smiles)
    if mol is None:
        return None
    from rdkit import Chem
    from rdkit.Chem.MolStandardize import rdMolStandardize
    try:
        mol = rdMolStandardize.ChargeParent(mol)
    except Exception:
        pass  # fall back to the as-typed structure
    return Chem.MolToSmiles(mol)
