"""Unit tests for core/identity_resolver.py.

Gemini is stubbed (no network, no cost) and the index is a tiny synthetic SQLite, so
these exercise the orchestrator logic: report-aware candidate pick, selection-only
synonym filtering, status mapping, cross-ref extraction, and RDKit hydration.
"""

import json
import re
import sqlite3
from unittest.mock import patch

import pytest

from core import identity_resolver
from core.pubchem_offline import PubChemOffline

# Reuse the collision fixture: "C1632" -> cefotaxime (2153) AND a LIN28 inhibitor
# (91870554, modelled here as benzoic acid C7H6O2). Imatinib (5291) + its mesylate salt.
ROWS_SYN = [
    (5291, "Imatinib", "imatinib"), (5291, "Gleevec", "gleevec"), (5291, "STI571", "sti571"),
    (5291, "152459-95-5", "152459955"), (5291, "CHEMBL941", "chembl941"),
    (123596, "Imatinib mesylate", "imatinibmesylate"),
    (2153, "Cefotaxime", "cefotaxime"), (2153, "C1632", "c1632"),
    (91870554, "C1632", "c1632"), (91870554, "LIN28 inhibitor", "lin28inhibitor"),
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


def _stub_gemini(pick_behaviour="lin28", inject_bad_synonym=None):
    """Return a fake generate_with_search. ``pick_behaviour`` controls the candidate
    pick; the filter echoes the numbered input (optionally injecting a bogus synonym to
    prove the selection-only intersection drops it)."""
    def fake(prompt, model_name=None, generation_config_overrides=None, use_search=True):
        if "curating the list of search aliases" in prompt:  # synonym filter prompt
            kept = re.findall(r"^\d+\.\s+(.*)$", prompt, flags=re.M)
            if inject_bad_synonym:
                kept = kept + [inject_bad_synonym]
            return {"text": json.dumps(kept), "usage": {}}
        # candidate-pick prompt
        blocks = re.findall(r"^\[(\d+)\]", prompt, flags=re.M)
        if pick_behaviour == "none":
            return {"text": json.dumps({"choice": None, "status": "unresolved",
                                        "reason": "no candidate fits"}), "usage": {}}
        if pick_behaviour == "out_of_scope":
            return {"text": json.dumps({"choice": None, "status": "out_of_scope",
                                        "out_of_scope_reason": "biologic"}), "usage": {}}
        if pick_behaviour == "lin28":
            # choose the candidate block that mentions LIN28 (uses the report's mechanism)
            idx = 0
            for m in re.finditer(r"^\[(\d+)\][^\[]*", prompt, flags=re.M):
                if "LIN28" in m.group(0):
                    idx = int(m.group(1))
            return {"text": json.dumps({"choice": idx, "status": "resolved",
                                        "reason": "matches the LIN28 mechanism"}), "usage": {}}
        # default: first block
        return {"text": json.dumps({"choice": int(blocks[0]), "status": "resolved"}), "usage": {}}
    return fake


def test_report_disambiguates_collision(monkeypatch, index):
    """C1632 collides on cefotaxime + a LIN28 inhibitor; the report says LIN28, so we
    must pick the LIN28 compound and never carry 'cefotaxime' as a synonym."""
    monkeypatch.setattr(identity_resolver, "generate_with_search", _stub_gemini("lin28"))
    report = "Verdict: VERIFIED. Target: LIN28. C1632 is a small-molecule LIN28 inhibitor."
    res = identity_resolver.resolve_identity_from_report("C1632", report, index=index, write=False)
    assert res["status"] == "resolved"
    assert res["identity"]["parent_cid"] == "91870554"
    syn_lower = {s.lower() for s in res["synonyms"]}
    assert "cefotaxime" not in syn_lower
    assert res["identity"]["formula"] == "C7H6O2"  # RDKit on the LIN28 stand-in (benzoic acid)


def test_selection_only_drops_injected_synonym(monkeypatch, index):
    """The filter is hallucination-proof: a synonym the model returns that wasn't in the
    input is dropped by the intersection."""
    monkeypatch.setattr(identity_resolver, "generate_with_search",
                        _stub_gemini("lin28", inject_bad_synonym="TotallyMadeUpName"))
    report = "Target: LIN28 inhibitor."
    res = identity_resolver.resolve_identity_from_report("C1632", report, index=index, write=False)
    assert "TotallyMadeUpName" not in res["synonyms"]


def test_no_candidates_is_unresolved(monkeypatch, index):
    called = []
    monkeypatch.setattr(identity_resolver, "generate_with_search",
                        lambda *a, **k: called.append(1) or {"text": "{}", "usage": {}})
    res = identity_resolver.resolve_identity_from_report("NoSuchDrug999", "x", index=index, write=False)
    assert res["status"] == "unresolved"
    assert res["identity"] is None
    assert not called  # we never call the LLM when there are zero candidates


def test_pick_none_is_unresolved(monkeypatch, index):
    monkeypatch.setattr(identity_resolver, "generate_with_search", _stub_gemini("none"))
    res = identity_resolver.resolve_identity_from_report("Imatinib", "x", index=index, write=False)
    assert res["status"] == "unresolved" and res["identity"] is None


def test_pick_out_of_scope(monkeypatch, index):
    monkeypatch.setattr(identity_resolver, "generate_with_search", _stub_gemini("out_of_scope"))
    res = identity_resolver.resolve_identity_from_report("Imatinib", "x", index=index, write=False)
    assert res["status"] == "out_of_scope" and res["identity"] is None


def test_hydration_and_xrefs(monkeypatch, index):
    """Imatinib resolves; cross-refs (CAS, ChEMBL) are scraped from synonyms and the
    structure is hydrated via RDKit."""
    monkeypatch.setattr(identity_resolver, "generate_with_search", _stub_gemini("lin28"))
    res = identity_resolver.resolve_identity_from_report(
        "Imatinib", "Target: BCR-ABL tyrosine kinase.", index=index, write=False)
    assert res["status"] == "resolved"
    ident = res["identity"]
    assert ident["cas"] == "152459-95-5"
    assert ident["chembl_id"] == "CHEMBL941"
    assert ident["inchikey"] and len(ident["inchikey"]) == 27       # full InChIKey
    assert ident["inchikey_skeleton"] == ident["inchikey"][:14]
    assert ident["formula"].startswith("C29")                       # imatinib free base


def test_xref_extraction_patterns():
    syns = ["Imatinib", "152459-95-5", "CHEMBL941", "DB00619", "CHEBI:45783", "7RN5DR86CK", "junk"]
    x = identity_resolver._extract_xrefs(syns)
    assert x["cas"] == "152459-95-5"
    assert x["chembl_id"] == "CHEMBL941"
    assert x["drugbank_id"] == "DB00619"
    assert x["chebi_id"] == "CHEBI:45783"
    assert x["unii"] == "7RN5DR86CK"


# --- _store() -- inline SMILES canonicalization on the write path (write=True) ----------------
# All tests above use write=False. These use the real per-test isolated DB (tests/pipeline/conftest.py)
# and exercise _store()'s inline canonicalization: same session, same transaction, no network.

def test_write_true_canonicalizes_smiles_inline(monkeypatch, index):
    monkeypatch.setattr(identity_resolver, "generate_with_search", _stub_gemini("lin28"))
    report = "Verdict: VERIFIED. Target: LIN28. C1632 is a small-molecule LIN28 inhibitor."
    res = identity_resolver.resolve_identity_from_report("C1632", report, index=index, write=True)
    assert res["status"] == "resolved"

    from core.db import init_db, DrugSynonym
    from core.smiles_canonicalize import canonicalize
    Session = init_db()
    s = Session()
    try:
        row = s.query(DrugSynonym).filter(DrugSynonym.drug_name == "C1632").first()
        assert row is not None and row.smiles
        # canonicalized inline from the index-provided SMILES -- no network
        assert row.canonicalized_smiles == canonicalize(row.smiles)
    finally:
        s.close()


def test_write_true_persists_row_despite_canonicalize_failure(monkeypatch, index):
    """A canonicalization exception must not prevent the identity row from being committed --
    it is wrapped in its own try/except inside _store()."""
    monkeypatch.setattr(identity_resolver, "generate_with_search", _stub_gemini("lin28"))
    report = "Target: LIN28 inhibitor."
    with patch("core.identity_resolver.canonicalize", side_effect=RuntimeError("boom")):
        res = identity_resolver.resolve_identity_from_report("C1632", report, index=index, write=True)
    assert res["status"] == "resolved"

    from core.db import init_db, DrugSynonym
    Session = init_db()
    s = Session()
    try:
        row = s.query(DrugSynonym).filter(DrugSynonym.drug_name == "C1632").first()
        assert row is not None
        assert row.status == "resolved"
    finally:
        s.close()


def test_unresolved_never_canonicalizes(monkeypatch, index):
    """status != resolved goes through _persist_unresolved, not _store -- so the inline
    canonicalization (which lives inside _store) must never run for these."""
    monkeypatch.setattr(identity_resolver, "generate_with_search", _stub_gemini("none"))
    with patch("core.identity_resolver.canonicalize") as mock_canon:
        res = identity_resolver.resolve_identity_from_report("Imatinib", "x", index=index, write=True)
    assert res["status"] == "unresolved"
    mock_canon.assert_not_called()
