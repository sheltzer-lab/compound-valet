"""Unit tests for core/pubchem_offline.py over a tiny synthetic index.

No dependency on the multi-GB real flat files — we build a few-row SQLite that
mirrors the real schema and exercise the reader's logic (name->candidates,
salt-union, collisions, normalization).
"""

import sqlite3

import pytest

from core.pubchem_offline import PubChemOffline

# CIDs: 5291 Imatinib (free base), 123596 imatinib mesylate (salt -> parent 5291),
#       2153 cefotaxime, 91870554 a LIN28 inhibitor; the code "C1632" collides on both.
ROWS_SYN = [
    (5291, "Imatinib", "imatinib"),
    (5291, "Gleevec", "gleevec"),
    (5291, "STI571", "sti571"),
    (5291, "152459-95-5", "152459955"),
    (5291, "CHEMBL941", "chembl941"),
    (123596, "Imatinib mesylate", "imatinibmesylate"),
    (123596, "Gleevec mesylate", "gleevecmesylate"),
    (2153, "Cefotaxime", "cefotaxime"),
    (2153, "C1632", "c1632"),
    (91870554, "C1632", "c1632"),
    (91870554, "LIN28 inhibitor", "lin28inhibitor"),
]
ROWS_PARENT = [(5291, 5291), (123596, 5291), (2153, 2153), (91870554, 91870554)]
ROWS_SMILES = [
    (5291, "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1"),
    (123596, "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1.CS(=O)(=O)O"),
    (2153, "CON=C(C(=O)NC1C(=O)N2C(C(=O)O)=C(COC(C)=O)CSC12)c1csc(N)n1"),
    (91870554, "O=C(O)c1ccccc1"),
]


@pytest.fixture
def index(tmp_path):
    db = tmp_path / "mini.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE syn(cid INTEGER, syn TEXT, syn_norm TEXT);"
        "CREATE TABLE cid_parent(cid INTEGER PRIMARY KEY, parent_cid INTEGER);"
        "CREATE TABLE cid_smiles(cid INTEGER PRIMARY KEY, smiles TEXT);"
    )
    con.executemany("INSERT INTO syn VALUES (?,?,?)", ROWS_SYN)
    con.executemany("INSERT INTO cid_parent VALUES (?,?)", ROWS_PARENT)
    con.executemany("INSERT INTO cid_smiles VALUES (?,?)", ROWS_SMILES)
    con.executescript(
        "CREATE INDEX i1 ON syn(syn_norm); CREATE INDEX i2 ON syn(cid);"
        "CREATE INDEX i3 ON cid_parent(parent_cid);"
    )
    con.commit()
    con.close()
    return PubChemOffline(str(db))


def test_candidates_basic(index):
    assert index.candidates_for_name("Imatinib") == [5291]
    assert index.candidates_for_name("Gleevec") == [5291]
    assert index.candidates_for_name("STI571") == [5291]


def test_candidates_normalize_case_and_punct(index):
    # normalize_key lowercases + strips non-alphanumerics
    assert index.candidates_for_name("GLEEVEC") == [5291]
    assert index.candidates_for_name("sti-571") == [5291]


def test_collision_returns_both(index):
    # "C1632" is (deliberately) attached to two unrelated compounds
    assert set(index.candidates_for_name("C1632")) == {2153, 91870554}


def test_unknown_name_empty(index):
    assert index.candidates_for_name("not-a-drug-xyz") == []
    assert index.candidates_for_name("") == []


def test_parent_and_salt_union(index):
    assert index.parent_cid(123596) == 5291      # mesylate -> free base
    assert index.parent_cid(5291) == 5291         # parent of itself
    assert index.parent_cid(999999) == 999999     # missing -> itself
    assert index.salt_union_cids(5291) == {5291, 123596}
    assert index.salt_union_cids(123596) == {5291, 123596}  # same union from the salt


def test_salt_union_synonyms_spans_forms(index):
    syns = index.salt_union_synonyms(5291)
    assert "Imatinib" in syns and "Gleevec" in syns and "STI571" in syns
    assert "Imatinib mesylate" in syns            # pulled from the salt sibling


def test_synonyms_for_cids_dedup_order(index):
    out = index.synonyms_for_cids([5291, 5291])
    assert out.count("Imatinib") == 1             # deduped


def test_smiles_lookup(index):
    assert index.smiles_for_cid(5291).startswith("Cc1ccc")
    assert index.smiles_for_cid(404) is None


def test_missing_index_raises(tmp_path):
    idx = PubChemOffline(str(tmp_path / "nope.sqlite"))
    with pytest.raises(FileNotFoundError):
        idx.candidates_for_name("Imatinib")
