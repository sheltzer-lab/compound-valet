"""Tests for core/smiles_canonicalize.py — the shared canonicalizer identity resolution uses
for drug_synonyms.canonicalized_smiles."""
from core import smiles_canonicalize as sc


# --- canonicalize (as-drawn) -------------------------------------------------------

def test_canonicalize_is_stable_across_equivalent_forms():
    # Benzene written two different but chemically identical ways must canonicalize identically.
    a = sc.canonicalize("c1ccccc1")
    b = sc.canonicalize("C1=CC=CC=C1")
    assert a is not None and a == b


def test_canonicalize_is_idempotent():
    once = sc.canonicalize("CC(=O)OC1=CC=CC=C1C(=O)O")  # aspirin
    twice = sc.canonicalize(once)
    assert once == twice


def test_canonicalize_invalid_smiles_returns_none():
    assert sc.canonicalize("not a smiles $$$") is None
    assert sc.canonicalize("C(C") is None  # malformed ring closure
    assert sc.canonicalize("") is None
    assert sc.canonicalize(None) is None


# --- canonicalize_parent (salt-stripped) -------------------------------------------

def test_canonicalize_parent_strips_salt_to_free_compound():
    # Ethylamine hydrochloride -> ethylamine free base.
    salt_parent = sc.canonicalize_parent("CCN.Cl")
    free_base = sc.canonicalize("CCN")
    assert salt_parent is not None and salt_parent == free_base


def test_canonicalize_parent_of_non_salt_is_unchanged():
    # A compound with no counterion is already its own parent.
    as_drawn = sc.canonicalize("CC(=O)OC1=CC=CC=C1C(=O)O")
    parent = sc.canonicalize_parent("CC(=O)OC1=CC=CC=C1C(=O)O")
    assert as_drawn == parent


def test_canonicalize_parent_neutralizes_charge():
    # PP2's protonated cation (a real test case) must canonicalize, at the parent tier,
    # to the same structure as its neutral free-base form -- same compound, different point on a
    # pH equilibrium, not two different compounds the way a salt pairing is.
    cation = "CC(C)(C)n1[nH+]c(c2ccc(Cl)cc2)c3c(N)ncnc13"
    neutral = "NC1=C2C(N(C(C)(C)C)N=C2C3=CC=C(Cl)C=C3)=NC=N1"
    assert sc.canonicalize_parent(cation) == sc.canonicalize_parent(neutral)
    # And the parent tier must actually be neutral (charge=0), not just self-consistent.
    from rdkit import Chem
    mol = Chem.MolFromSmiles(sc.canonicalize_parent(cation))
    assert Chem.GetFormalCharge(mol) == 0


def test_canonicalize_parent_invalid_smiles_returns_none():
    assert sc.canonicalize_parent("garbage!!") is None
