"""Structure-anchored compound-identity accessor over the drug_synonyms table.

Reads the resolved identity table (DrugSynonym, keyed by drug_name exactly like analysis_results)
to surface a compound's canonical InChIKey identity + external cross-refs.

Identity model: canonical key = parent_inchikey (salts/hydrates/protonation merge; stereoisomers
stay distinct).

get_identity degrades gracefully when the drug_synonyms table is ABSENT or empty: it returns None
rather than raising.
"""
import logging

from sqlalchemy.exc import SQLAlchemyError

from core.db import init_db, DrugSynonym, stereo_key

logger = logging.getLogger(__name__)

# DrugSynonym columns surfaced as a plain dict (excludes the synthetic `id` PK).
IDENTITY_FIELDS = [
    "drug_name", "status", "resolved_via", "query_used", "cid", "inchikey",
    "inchikey_skeleton", "parent_cid", "parent_inchikey", "parent_inchikey_skeleton",
    "inchi", "iupac_name", "formula", "pubchem_title", "cas", "drugbank_id",
    "chembl_id", "unii", "chebi_id", "drugcentral_id", "n_synonyms", "sample_synonyms",
    "smiles", "parent_smiles",
]


def _to_dict(row):
    return {f: getattr(row, f, None) for f in IDENTITY_FIELDS}


def _with_session(fn, default):
    """Run fn(session) with a fresh session; return `default` if drug_synonyms is missing
    (no-such-table / undefined-relation) or any DB error occurs. Never raises."""
    Session = init_db()
    s = Session()
    try:
        return fn(s)
    except SQLAlchemyError as e:
        logger.debug("identity lookup skipped (%s — drug_synonyms unavailable?)", e.__class__.__name__)
        return default
    finally:
        s.close()


def get_identity(drug_name):
    """Return the drug_synonyms identity row for drug_name as a dict, or None.

    Exact match first, then a stereo_key() scan: case and punctuation are ignored, so
    'ots-964' finds the row keyed 'OTS964', but (+)/(-) are kept, so '(-)-X' never gets the
    identity of '(+)-X'."""
    if not drug_name:
        return None

    def q(s):
        r = s.query(DrugSynonym).filter_by(drug_name=drug_name).first()
        if r:
            return _to_dict(r)
        target = stereo_key(drug_name)
        for (name,) in s.query(DrugSynonym.drug_name).all():
            if stereo_key(name) == target:
                hit = s.query(DrugSynonym).filter_by(drug_name=name).first()
                return _to_dict(hit) if hit else None
        return None

    return _with_session(q, None)


