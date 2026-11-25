"""Tests for core/identity.py — the structure-anchored compound-identity accessor.

Uses the autouse per-test SQLite isolation in tests/pipeline/conftest.py; each test seeds its own
DrugSynonym rows.
"""
from core.db import init_db, DrugSynonym, AnalysisResult
import core.identity as identity

# Synthetic InChIKey.
IMA = "KTUFNOKKBVMGRW-UHFFFAOYSA-N"          # imatinib (free base; parent == self)


def _syn(drug_name, inchikey="", parent_inchikey="", status="resolved", **kw):
    d = dict(drug_name=drug_name, status=status, inchikey=inchikey,
             parent_inchikey=parent_inchikey)
    d.update(kw)
    return d


def _seed(synonyms=(), analyzed=()):
    Session = init_db()
    s = Session()
    try:
        for row in synonyms:
            s.add(DrugSynonym(**row))
        for nm in analyzed:
            s.add(AnalysisResult(drug_name=nm))
        s.commit()
    finally:
        s.close()


# --- get_identity -----------------------------------------------------------------

def test_get_identity_exact():
    _seed([_syn("OTS964", inchikey=IMA, parent_inchikey=IMA, cas="1338545-07-5")])
    r = identity.get_identity("OTS964")
    assert r and r["drug_name"] == "OTS964" and r["cas"] == "1338545-07-5"


def test_get_identity_normalize_fallback():
    _seed([_syn("OTS964", inchikey=IMA)])
    r = identity.get_identity("ots-964")   # case/sep-insensitive via normalize_key
    assert r and r["drug_name"] == "OTS964"


def test_get_identity_miss_returns_none():
    _seed([_syn("OTS964", inchikey=IMA)])
    assert identity.get_identity("nonexistent-drug") is None
    assert identity.get_identity("") is None


# --- graceful degradation when the table is absent --------------------------------

def test_accessors_safe_when_table_absent():
    # An older database may lack drug_synonyms; accessors must not raise.
    import core.db
    init_db()
    DrugSynonym.__table__.drop(core.db._ENGINE)   # simulate "table not in the existing DB"
    assert identity.get_identity("Imatinib") is None
