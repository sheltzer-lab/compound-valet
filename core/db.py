import os
import json
import re
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, JSON, DateTime, Boolean, event, Index, func, or_
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from core import scratch_db

load_dotenv()

# DB Setup
DB_NAME = "compound_valet.db"
# Use CWD to ensure we write to the expected data directory
DATA_DIR = os.path.join(os.getcwd(), "data")
DB_PATH = os.path.join(DATA_DIR, DB_NAME)

# Priority: Environment Variable > Local SQLite
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{DB_PATH}"


Base = declarative_base()

class VerifiedPaper(Base):
    __tablename__ = 'verified_papers'

    id = Column(Integer, primary_key=True)
    drug_name = Column(String, index=True) 
    title = Column(Text)
    authors = Column(Text) 
    year = Column(String)
    url = Column(Text)
    citation = Column(String) # Pre-computed citation (Smith, 2023)

    # DOI of the paper itself, as issued (not lowercased) — the identifier the
    # retraction check looks up (core/retraction_live.py). Sourced from the
    # S2/CrossRef match during verification (core/references/checker.py), falling
    # back to the URL. Nullable: papers whose DOI never resolved are NULL. No
    # index: nothing filters VerifiedPaper by doi.
    doi = Column(String)

    # Retraction status, sourced from CrossRef (which distributes the Retraction
    # Watch database). Tri-state, so NO default:
    #   True  = CrossRef reports a retraction notice for this paper
    #   False = CrossRef was read successfully and reports none
    #   None  = unknown (no DOI to look up, or the lookup failed)
    # None must persist as SQL NULL, never be coerced to False — a default here
    # would silently turn "unknown" into "checked & clean" on every insert.
    # Written by core/retraction_live.py during analysis.
    retracted = Column(Boolean)
    retraction_date = Column(String)         # ISO date of the retraction notice
    retraction_notice_doi = Column(String)   # DOI of the retraction notice

    created_at = Column(DateTime, default=datetime.utcnow)


class JobScopedVerifiedPaper(Base):
    """Papers belonging to a run whose report has NOT been saved yet.

    Writing a run's papers into `verified_papers` at ingestion deletes every
    existing row for the drug first. If the run's report is then never saved,
    the drug keeps its OLD report while its papers have been replaced, so the
    report cites papers the database no longer holds.

    So a batch run keeps its papers HERE, under its own scope, and
    `promote_job_papers` moves papers and report into place together in one
    transaction. `verified_papers` keeps exactly its previous
    meaning, so its readers need not know this table exists.

    Columns mirror VerifiedPaper so a row copies across field for field.
    """
    __tablename__ = 'job_scoped_verified_papers'

    id = Column(Integer, primary_key=True)
    # The owning run. Every way a run ends deletes these rows at that moment:
    # saved (promote_job_papers consumes them) or discarded. Only a hard kill
    # mid-run can orphan any, and those are inert — nothing reads this table
    # except for a specific job_id.
    job_id = Column(String, index=True, nullable=False)
    drug_name = Column(String, index=True)
    title = Column(Text)
    authors = Column(Text)
    year = Column(String)
    url = Column(Text)
    citation = Column(String)
    doi = Column(String)
    retracted = Column(Boolean)              # tri-state, as on VerifiedPaper
    retraction_date = Column(String)
    retraction_notice_doi = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# Columns copied between the two tables on promote. DERIVED from the model, not
# listed by hand: a hand-written list silently drops any column added later, and
# a dropped column here means data loss at promote. `id` and `created_at` belong
# to the row, not the paper, so they are not carried; `job_id` exists only on the
# job-scoped side.
_PAPER_FIELDS = tuple(
    c.name for c in VerifiedPaper.__table__.columns
    if c.name not in ("id", "created_at"))

# The twin must be able to hold everything VerifiedPaper does, or promote would
# lose a field. Checked at import so the two models cannot drift apart unnoticed.
_missing = set(_PAPER_FIELDS) - {c.name for c in JobScopedVerifiedPaper.__table__.columns}
assert not _missing, f"JobScopedVerifiedPaper is missing columns: {sorted(_missing)}"


class AnalysisResult(Base):
    __tablename__ = 'analysis_results'

    id = Column(Integer, primary_key=True)
    drug_name = Column(String, index=True, unique=True) # One report per drug for simplicity
    timestamp = Column(String)
    verdict = Column(String)
    
    # Reports
    pi_report = Column(Text)
    genetics_report = Column(Text)
    biochem_report = Column(Text)
    biophysics_report = Column(Text)
    
    # Data Blobs
    raw_context = Column(Text) # JSON string of papers or just text
    system_context = Column(Text)
    
    # Stats
    usage_stats = Column(JSON)
    cost = Column(Float)
    time = Column(Float)
    
    # [NEW] Structured Metadata (Original Target, True Target, Score, etc)
    structured_summary = Column(JSON)

    # Promoted columns from structured_summary (for lightweight queries)
    target = Column(String)                  # e.g. "Thymidylate Synthase"
    target_gene_names_list = Column(JSON)    # e.g. ["TYMS"] — named _list to avoid collision with drug_targets
    selectivity_grade = Column(String)       # e.g. "SELECTIVE"
    mechanism = Column(String)               # e.g. "Inhibitor" — PI-verified mechanism of action

    # Literature Critic & Calibration
    # Literature Critic & Calibration
    reviewer_reports = Column(JSON) # JSON Dict: { "Reviewer Name": "Report Text" }
    # lit_critic_report removed in favor of structured reviewer_reports
    citation_score = Column(Integer)
    citation_stats = Column(JSON)
    corrected_pi_report = Column(Text)


class DrugTarget(Base):
    """
    Junction table for efficient target gene lookups.
    Denormalized from structured_summary.target_gene_names for query performance.
    """
    __tablename__ = 'drug_targets'
    
    id = Column(Integer, primary_key=True)
    drug_name = Column(String, index=True, nullable=False)
    gene_name = Column(String, index=True, nullable=False)  # HGNC gene name
    
    # Composite index for efficient lookups
    __table_args__ = (
        Index('ix_drug_targets_gene_drug', 'gene_name', 'drug_name'),
    )


class DrugSynonym(Base):
    """Structure-anchored identity for each drug, resolved from its name via PubChem.

    One row per drug_name (the same key as analysis_results), written when the
    drug's identity is resolved (core/identity_resolver).
    Canonical identity = parent_inchikey (salts collapse, stereochemistry preserved);
    inchikey is the as-resolved form; resolution_status lives in `status`."""
    __tablename__ = 'drug_synonyms'
    id = Column(Integer, primary_key=True)
    drug_name = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, index=True)            # resolved / unresolved / out_of_scope / combination
    resolved_via = Column(String)                  # which rung/LLM kind resolved it (may carry ~stereo_flag)
    query_used = Column(String)
    cid = Column(String)                           # PubChem CID (provenance / link)
    inchikey = Column(String, index=True)          # as-resolved InChIKey
    inchikey_skeleton = Column(String, index=True) # 14-char scaffold block (stereo-blind; NOT a merge key)
    parent_cid = Column(String)
    parent_inchikey = Column(String, index=True)   # canonical key: desalted parent, stereo-preserving
    parent_inchikey_skeleton = Column(String, index=True)
    inchi = Column(Text)
    iupac_name = Column(Text)
    formula = Column(String)
    pubchem_title = Column(String)
    cas = Column(String)
    drugbank_id = Column(String)
    chembl_id = Column(String)
    unii = Column(String)
    chebi_id = Column(String)
    drugcentral_id = Column(String)
    n_synonyms = Column(Integer)
    sample_synonyms = Column(Text)
    smiles = Column(Text)            # isomeric SMILES (as-resolved), backfilled from PubChem
    parent_smiles = Column(Text)     # isomeric SMILES of the desalted parent
    canonicalized_smiles = Column(Text)         # RDKit-canonicalized as-drawn SMILES
    canonicalized_parent_smiles = Column(Text)  # RDKit-canonicalized, salt-stripped SMILES


class SynonymIndex(Base):
    """Alias -> analyzed-compound lookup (brand names, codes, salt names, registry IDs).

    Separate from drug_synonyms (one row per analyzed compound): this is one row per (alias,
    compound). Written by core/identity_resolver._store when a drug's identity is resolved: the
    PubChem synonyms of every salt form of the chosen compound, filtered by a model call that keeps
    only true aliases of that structure. Those rows have source 'pubchem:report-resolved' and
    confidence 'high'. Rows with source 'added by hand' or 'merged duplicate' are written by
    hand, not by this code, and every identity write keeps them (KEPT_SYNONYM_SOURCES).

    An alias is NOT unique: the same alias can point at several drugs. Salt forms of one compound
    share their aliases by design, so do compounds we hold twice, and a few aliases point at
    different compounds (a brand name reused for another product, say). Callers that need one
    drug must handle several rows. Read by core/identity."""
    __tablename__ = 'synonym_index'
    id = Column(Integer, primary_key=True)
    alias_norm = Column(String, index=True, nullable=False)   # normalize_key(alias) — the lookup key
    alias_raw = Column(String)                                # the alias as sourced
    canonical_drug_name = Column(String, index=True)          # the analyzed drug it points to
    parent_inchikey = Column(String, index=True)              # canonical structure key of that compound
    source = Column(String)                                   # pubchem:report-resolved | added by hand | merged duplicate
    confidence = Column(String)                               # high for resolver rows


def delete_verified_papers(session, drug_name: str, job_id: str = None):
    """Drop a drug's papers. With `job_id`, drops only that job's job-scoped rows —
    the live `verified_papers` table is left alone."""
    if job_id:
        session.query(JobScopedVerifiedPaper).filter_by(
            drug_name=drug_name, job_id=job_id).delete()
    else:
        session.query(VerifiedPaper).filter_by(drug_name=drug_name).delete()

def save_verified_papers(session, drug_name: str, papers: list, job_id: str = None):
    """Replace a drug's papers.

    With `job_id` the rows go into `job_scoped_verified_papers` and
    `verified_papers` is not touched, so a run whose report is not saved cannot
    disturb the saved report. Without it, the drug's saved papers are replaced.
    """
    delete_verified_papers(session, drug_name, job_id)
    for p in papers:
        # Robust handling of authors list
        authors_raw = p.get('authors', [])
        final_authors = "Unknown"

        if isinstance(authors_raw, list):
            # It's now reliably a list of strings from checker
            final_authors = ", ".join(str(a) for a in authors_raw)
        elif isinstance(authors_raw, str):
            final_authors = authors_raw


        fields = dict(
            drug_name=drug_name,
            title=p.get('title', 'Unknown'),
            authors=final_authors,
            year=str(p.get('year', 'Unknown')),
            url=p.get('url', 'Unknown'),
            citation=p.get('citation', 'Unknown'),
            # Resolved by the citation manager alongside the retraction flag below;
            # None when neither the verified match nor the URL yielded one.
            doi=p.get('doi'),
            # Tri-state retraction flag set by the citation manager (True/False/None).
            retracted=p.get('retracted'),
            retraction_date=p.get('retraction_date'),
            retraction_notice_doi=p.get('retraction_notice_doi'),
        )
        row = (JobScopedVerifiedPaper(job_id=job_id, **fields) if job_id
               else VerifiedPaper(**fields))
        session.add(row)

def save_papers_standalone(drug_name: str, papers: list, job_id: str = None):
    """
    Saves verified papers to the database without requiring an active session.
    Used during ingestion to persist papers before full analysis is complete.

    `job_id` scopes them to that job instead of replacing the live rows;
    see JobScopedVerifiedPaper.
    """
    Session = init_db()
    session = Session()
    try:
        save_verified_papers(session, drug_name, papers, job_id)
        session.commit()
    finally:
        session.close()


def _identity_row_to_dict(obj):
    """Row -> plain column dict, dropping the autoincrement id so it can be re-inserted."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name != "id"}


def get_identity_rows(drug_name: str) -> dict:
    """Capture the resolved identity a pipeline run wrote — the drug_synonyms row plus its
    synonym_index rows — from the CURRENT DB, as plain column dicts. The identity analogue of
    get_verified_papers: consensus captures this from a run's scratch DB (the run resolves
    identity with write=True) so the winner's OWN resolved identity can
    be persisted at promote without re-running the resolver (which re-invokes the LLM and can
    diverge from / fail against the report). Returns
    {"drug_synonyms": {...}|None, "synonym_index": [ {...}, ... ]}. Best-effort — any error
    yields empty identity rather than breaking the run."""
    Session = init_db()
    session = Session()
    try:
        ds = session.query(DrugSynonym).filter_by(drug_name=drug_name).first()
        si = session.query(SynonymIndex).filter_by(canonical_drug_name=drug_name).all()
        return {"drug_synonyms": _identity_row_to_dict(ds) if ds else None,
                "synonym_index": [_identity_row_to_dict(r) for r in si]}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("identity capture skipped for %s: %r", drug_name, e)
        return {"drug_synonyms": None, "synonym_index": []}
    finally:
        session.close()


# synonym_index rows that no resolver wrote: an alias added by hand, or the name of a
# duplicate merged into this drug. Writing a fresh identity for the drug keeps them.
KEPT_SYNONYM_SOURCES = ("added by hand", "merged duplicate")


def is_resolved_identity(row) -> bool:
    """A drug_synonyms row (or column dict) that pins a structure: an inchikey or a cid.

    The status is not checked: a row that holds a structure is kept like a resolved one,
    whatever its status."""
    get = row.get if isinstance(row, dict) else (lambda k: getattr(row, k, None))
    return bool(get("inchikey") or get("cid"))


def keeps_resolved_identity(existing, new_status) -> bool:
    """Would writing an identity of `new_status` over `existing` throw a resolved one away?

    A re-resolution that finds nothing (unresolved / out_of_scope / combination) must not
    replace one that did. Every identity writer checks this and, when it is True, leaves
    the resolved row and its synonyms as they are."""
    return (existing is not None and new_status != "resolved"
            and is_resolved_identity(existing))


def identity_row(session, drug_name: str):
    """The drug's drug_synonyms row: the one spelled exactly so, else one that differs only
    in case (identity_resolver._store has always matched case-insensitively). Exact first,
    so a write for "Tovexin" never lands on a separate "TOVEXIN" row."""
    return (session.query(DrugSynonym).filter_by(drug_name=drug_name).first()
            or session.query(DrugSynonym)
            .filter(func.lower(DrugSynonym.drug_name) == drug_name.lower()).first())


def replace_synonym_index(session, names, rows: list) -> None:
    """Swap a drug's synonym_index rows for `rows` in an EXISTING session. Does NOT commit.

    `names` are the drug's spellings whose rows are replaced: the run's own and its
    drug_synonyms row's, which can differ in case (see identity_row).

    Rows whose source is in KEPT_SYNONYM_SOURCES stay, and an incoming alias they already
    hold is not added again: "ZB-77" is not added beside a hand-added "ZB 77". Compared by
    alias_norm with its (+)/(-) sign kept (stereo_key), because alias_norm
    alone drops the sign, so a hand-added "Nutlin-3" would hide Nutlin-3a's "(-)-Nutlin-3",
    a different compound."""
    mine = session.query(SynonymIndex).filter(SynonymIndex.canonical_drug_name.in_(set(names)))
    kept = {stereo_key(r.alias_raw)
            for r in mine.filter(SynonymIndex.source.in_(KEPT_SYNONYM_SOURCES))}
    # NOT IN is never true for a NULL source, so those rows need their own test.
    mine.filter(or_(SynonymIndex.source.is_(None),
                    SynonymIndex.source.notin_(KEPT_SYNONYM_SOURCES))
                ).delete(synchronize_session=False)
    for r in rows:
        if stereo_key(r.get("alias_raw")) not in kept:
            session.add(SynonymIndex(**r))


def _write_identity(session, drug_name: str, identity: dict) -> None:
    """Write identity rows (shaped like get_identity_rows) into an EXISTING session.
    Does NOT commit.

    An upsert, like identity_resolver._store: the columns `identity` carries overwrite the
    row's, and columns it does not carry stay. A resolved identity is never replaced by a
    non-resolved one, and hand-added and merged-duplicate synonyms survive. No drug_synonyms
    row in `identity` means there is nothing to write."""
    new = (identity or {}).get("drug_synonyms")
    if not new:
        return
    row = identity_row(session, drug_name)
    if keeps_resolved_identity(row, new.get("status")):
        print(f"⚠️ {drug_name}: run's identity is {new.get('status')}; keeping the "
              f"resolved identity already stored")
        return
    if row is None:
        row = DrugSynonym(drug_name=drug_name)
        session.add(row)
    for k, v in new.items():
        if k == "drug_name":            # an existing row keeps its own spelling
            continue
        setattr(row, k, v)
    replace_synonym_index(session, {drug_name, row.drug_name},
                          identity.get("synonym_index") or [])


def save_identity_rows(drug_name: str, identity: dict) -> None:
    """Persist captured identity rows (from get_identity_rows) into the target DB, by the
    rules in _write_identity. The identity analogue of save_papers_standalone — no
    LLM/network, it just writes what the winning run already resolved. No-op if the run
    carried no identity."""
    if not identity or not identity.get("drug_synonyms"):
        return
    Session = init_db()
    session = Session()
    try:
        _write_identity(session, drug_name, identity)
        session.commit()
    finally:
        session.close()


def get_verified_papers(drug_name: str, job_id: str = None) -> list:
    """
    Retrieves verified papers from the database for a given drug.
    Returns a list of paper dictionaries.

    `job_id` reads that run's own papers instead of the live ones, so a run
    whose report is not saved yet sees its OWN papers throughout — the model,
    the report it writes, and the papers eventually promoted all agree.
    """
    Session = init_db()
    session = Session()
    model = JobScopedVerifiedPaper if job_id else VerifiedPaper
    q = session.query(model).filter_by(drug_name=drug_name)
    if job_id:
        q = q.filter_by(job_id=job_id)
    papers = q.order_by(model.id).all()
    session.close()
    
    result = []
    for p in papers:
        authors_str = p.authors or "Unknown"
        # Authors stay one string: splitting on commas would break "Last, First" names
        result.append({
            "title": p.title or "Unknown",
            "authors": authors_str,
            "year": p.year or "Unknown",
            "url": p.url or "Unknown",
            "citation": p.citation,  # Return pre-computed citation
            "source_angle": "Verified",  # Mark as verified from DB
            # Must be the stored value, not None: callers round-trip this dict
            # back through save_papers_standalone (core/pipeline.py's missing-papers
            # merge), and save_verified_papers deletes before reinserting — so a
            # hardcoded None here would blank the column for the whole drug.
            "doi": p.doi,
            "retracted": p.retracted,                       # True / False / None
            "retraction_date": p.retraction_date,
            "retraction_notice_doi": p.retraction_notice_doi,
        })
    
    return result


# Global singleton for the engine
_ENGINE = None

def init_db():
    global _ENGINE

    # Per-run scratch override (consensus isolation); unset on every existing path.
    # Machinery lives in core.scratch_db — only the override belongs here.
    scratch_url = scratch_db.current()
    if scratch_url:
        return scratch_db.sessionmaker_for(scratch_url)

    # Return existing engine if already created
    if _ENGINE:
        return sessionmaker(bind=_ENGINE)

    # Otherwise create it
    if not DATABASE_URL.startswith("sqlite"):
        raise ValueError("DATABASE_URL must be a SQLite URL, such as sqlite:///data/compound_valet.db")
    print("🔌 DB Connection: Local SQLite")
    db_file = make_url(DATABASE_URL).database
    if db_file and db_file != ":memory:":
        parent_dir = os.path.dirname(os.path.abspath(db_file))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
    # This process is single-writer but heavily multi-threaded: reads in some
    # threads overlap writes (save_analysis) in others. In the default rollback
    # journal mode a writer's commit briefly locks the whole file; without a
    # busy timeout an overlapping reader raises 'database is locked' immediately
    # (intermittently — only when a read hits the commit window). `timeout` sets
    # SQLite's busy_timeout so a reader WAITS out the write instead of erroring.
    # Deliberately NOT using WAL: WAL adds -wal/-shm sidecar files, and copying or
    # moving the database file without them corrupts it. Keep it a one-file DB.
    connect_args = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(DATABASE_URL, connect_args=connect_args)

    # Create tables if they don't exist
    # Wrap in try/except to handle race conditions with multiple workers
    try:
        Base.metadata.create_all(engine)
    except Exception as e:
        # Ignore "table already exists" errors which happen during parallel init,
        # but never swallow real errors (e.g. unwritable path, corrupt file).
        if "already exists" not in str(e):
            engine.dispose()
            raise
    _ENGINE = engine
    return sessionmaker(bind=_ENGINE)

def _write_analysis(session, drug_name: str, data: dict):
    """Write one drug's analysis row into an EXISTING session. Does NOT commit.

    Split out of save_analysis so promote_job_papers can put the papers and the
    report in a single transaction. Two separate commits is its own route into
    the bug this module's JobScopedVerifiedPaper exists to prevent: a crash
    between them leaves the drug with new papers and the old report.
    """
    # Check existing
    existing = session.query(AnalysisResult).filter_by(drug_name=drug_name).first()

    # Extract promoted fields from structured_summary
    ss = data.get('structured_summary') or {}
    if not isinstance(ss, dict):
        if isinstance(ss, list) and len(ss) > 0 and isinstance(ss[0], dict):
            ss = ss[0]
        else:
            ss = {}

    if existing:
        # Update
        # UTC, always: with a naive local clock, `timestamp` values written on machines in
        # different time zones cannot be compared. Keep it naive-UTC so the stored format is
        # unchanged and every reader stays correct.
        existing.timestamp = datetime.utcnow().isoformat()

        existing.pi_report = data.get('report')
        existing.genetics_report = data.get('genetics_report')
        existing.biochem_report = data.get('biochem_report')
        existing.biophysics_report = data.get('biophysics_report')
        existing.raw_context = data.get('raw_context')
        existing.system_context = data.get('context')
        existing.usage_stats = data.get('usage')
        existing.cost = data.get('cost')
        existing.time = data.get('time')
        existing.structured_summary = data.get('structured_summary')

        # Promoted columns from structured_summary
        existing.target = ss.get('target')
        existing.target_gene_names_list = ss.get('target_gene_names')
        existing.selectivity_grade = ss.get('selectivity_grade')
        existing.mechanism = ss.get('mechanism')
        # Prefer structured verdict over regex extraction
        structured_verdict = ss.get('final_verdict')
        existing.verdict = structured_verdict if structured_verdict else extract_verdict(data.get('report', ''))

        # Lit Critic Updates
        existing.reviewer_reports = data.get('reviewer_reports')
        # The new run's citation results, as a new row gets them
        existing.citation_score = data.get('citation_score')
        existing.citation_stats = data.get('citation_stats')
        existing.corrected_pi_report = data.get('corrected_pi_report')

    else:

        # Create
        structured_verdict = ss.get('final_verdict')
        new_result = AnalysisResult(
            drug_name=drug_name,
            timestamp=datetime.utcnow().isoformat(),  # UTC, always — see the update branch above
            verdict=structured_verdict if structured_verdict else extract_verdict(data.get('report', '')),
            pi_report=data.get('report'),
            genetics_report=data.get('genetics_report'),
            biochem_report=data.get('biochem_report'),
            biophysics_report=data.get('biophysics_report'),
            raw_context=data.get('raw_context'),
            system_context=data.get('context'),
            usage_stats=data.get('usage'),
            cost=data.get('cost'),
            time=data.get('time'),
            structured_summary=data.get('structured_summary'),
            # Promoted columns
            target=ss.get('target'),
            target_gene_names_list=ss.get('target_gene_names'),
            selectivity_grade=ss.get('selectivity_grade'),
            mechanism=ss.get('mechanism'),
            # Store reviewer reports directly as JSON
            reviewer_reports=data.get('reviewer_reports'),
            citation_score=data.get('citation_score'),
            citation_stats=data.get('citation_stats'),
            corrected_pi_report=data.get('corrected_pi_report')
        )
        session.add(new_result)

    # No commit here: the caller owns the transaction.


def save_analysis(drug_name: str, data: dict):
    """Write one drug's analysis row and commit it."""
    Session = init_db()
    session = Session()
    try:
        _write_analysis(session, drug_name, data)
        session.commit()

        # Sync targets
        try:
            _sync_drug_targets(session, drug_name, data.get('structured_summary'))
            session.commit()
        except Exception as e:
            print(f"⚠️ Error syncing drug targets for {drug_name}: {e}")
            session.rollback()
    finally:
        session.close()


def _run_found_no_papers(data: dict) -> bool:
    """Did this run verify zero papers, as opposed to having lost its rows?

    `raw_context` is what the agents were actually shown, one `[Source](url)`
    line per paper (core/context.py's get_agent_context). No Source lines means
    the run had nothing — the two cases look identical from the row count alone,
    and they need opposite handling on promote.
    """
    raw = (data or {}).get("raw_context")
    if not raw:
        return False          # unknown, so take the cautious branch
    return "[Source](" not in raw


def promote_job_papers(drug_name: str, data: dict, job_id: str = None,
                       papers: list = None) -> None:
    """Put a run's papers and its report into place TOGETHER, in one transaction.

    Where the papers come from, in order:
      * `papers`  — an explicit list. Consensus captures the winner's papers
                    from its own scratch DB, so it has no rows here.
      * `job_id`  — rows written by that run in job_scoped_verified_papers.
      * neither   — the report is written alone; the drug's papers are left
                    as they are.

    The drug's identity is left alone (consensus writes the winner's with
    save_identity_rows).

    This deliberately does not call save_analysis, which commits on its own.
    Papers and report land together or not at all.
    """
    Session = init_db()
    session = Session()
    try:
        if papers is None and job_id:
            scoped = (session.query(JobScopedVerifiedPaper)
                      .filter_by(drug_name=drug_name, job_id=job_id)
                      .order_by(JobScopedVerifiedPaper.id).all())
            if scoped:
                papers = [{f: getattr(s, f) for f in _PAPER_FIELDS} for s in scoped]
            elif _run_found_no_papers(data):
                # The run genuinely verified nothing — its report says so, and
                # saying "no verified literature" beside a drug's old papers is
                # its own kind of mismatch. Replace with the empty set so the
                # report and the papers still describe the same run.
                papers = []
            else:
                # The report cites papers but this scope has no rows (an earlier
                # save consumed them). Leave the saved papers alone and write the
                # report only.
                print(f"⚠️ promote_job_papers: job {job_id} has no papers of its own "
                      f"for {drug_name}, but its report cites some; writing the "
                      f"report only and leaving verified_papers intact")

        if papers is not None:
            save_verified_papers(session, drug_name, papers)

        _write_analysis(session, drug_name, data)

        if job_id:
            session.query(JobScopedVerifiedPaper).filter_by(job_id=job_id).delete()

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Target sync is best-effort and deliberately outside the transaction above,
    # matching save_analysis: a failure here must not undo the promotion.
    Session2 = init_db()
    s2 = Session2()
    try:
        _sync_drug_targets(s2, drug_name, data.get('structured_summary'))
        s2.commit()
    except Exception as e:
        print(f"⚠️ Error syncing drug targets for {drug_name}: {e}")
        s2.rollback()
    finally:
        s2.close()


def discard_job_papers(job_id: str) -> int:
    """Drop one job's papers.

    Called when a run ends without being saved. Saving consumes the rows
    instead, inside promote_job_papers' transaction."""
    Session = init_db()
    session = Session()
    try:
        n = session.query(JobScopedVerifiedPaper).filter_by(job_id=job_id).delete()
        session.commit()
        return n
    finally:
        session.close()


def _sync_drug_targets(session, drug_name: str, structured_summary: dict):
    """
    Syncs the drug_targets junction table from structured_summary.target_gene_names.
    Called automatically when saving analysis.
    Only HGNC gene symbols (from target_gene_names) are stored — not aliases
    from the free-text target field.
    """
    # Delete existing targets for this drug
    session.query(DrugTarget).filter_by(drug_name=drug_name).delete()

    if not structured_summary or not isinstance(structured_summary, dict):
        return

    gene_names = structured_summary.get('target_gene_names', [])

    added_genes = set()

    if isinstance(gene_names, list):
        for gene in gene_names:
            if gene and isinstance(gene, str):
                normalized = gene.strip().upper()
                if normalized and normalized not in added_genes:
                    session.add(DrugTarget(drug_name=drug_name, gene_name=normalized))
                    added_genes.add(normalized)


def get_verified_selective_drugs_by_target(target_gene: str) -> list:
    """
    Get verified, selective drugs targeting a specific gene.
    Used to suggest alternatives for problematic compounds.

    Returns list of drug names (strings).
    """
    Session = init_db()
    session = Session()

    try:
        target_upper = target_gene.strip().upper()

        # Find drugs targeting this gene
        matching_drugs = (
            session.query(DrugTarget.drug_name)
            .filter(DrugTarget.gene_name == target_upper)
            .distinct()
            .all()
        )
        
        drug_names = [d[0] for d in matching_drugs]
        
        if not drug_names:
            return []
        
        # Filter for VERIFIED + SELECTIVE using promoted columns (no structured_summary blob)
        results = (
            session.query(AnalysisResult.drug_name)
            .filter(
                AnalysisResult.drug_name.in_(drug_names),
                func.upper(AnalysisResult.verdict) == 'VERIFIED',
                func.upper(AnalysisResult.selectivity_grade) == 'SELECTIVE',
            )
            .all()
        )

        return sorted(r[0] for r in results)

    finally:
        session.close()


def get_analysis(drug_name: str):
    Session = init_db()
    session = Session()
    result = session.query(AnalysisResult).filter_by(drug_name=drug_name).first()
    session.close()

    if result:
        return _result_to_dict(result)
    return None


def extract_verdict(report_text: str) -> str:
    """Simple helper to parse the Verdict line from the markdown."""
    if not report_text: return "Unknown"
    for line in report_text.split('\n'):
        if "**Verdict**" in line or "Verdict:" in line:
            return line.split(":")[-1].strip().replace("*", "")
    return "Unknown"


_GREEK_TO_ASCII = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta",
    "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu",
    "ν": "nu", "ξ": "xi", "π": "pi", "ρ": "rho",
    "σ": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi",
    "χ": "chi", "ψ": "psi", "ω": "omega",
    # The micro sign (U+00B5) looks like Greek mu (U+03BC) and papers use both: "4µ8c" must
    # meet our "4μ8C".
    "\u00b5": "mu",
}


def normalize_key(text: str) -> str:
    """Normalizes text for fuzzy comparison: lower case, remove non-alphanumeric.

    Greek Unicode letters (e.g. κ, α, β) are transliterated to ASCII equivalents
    before stripping, so "NF-κB" and "NF-kappaB" both normalize to "nfkappab".
    """
    if not text: return ""
    t = text.lower()
    for greek, ascii_eq in _GREEK_TO_ASCII.items():
        if greek in t:
            t = t.replace(greek, ascii_eq)
    return re.sub(r'[^a-z0-9]', '', t)

_SIGN_RE = re.compile(r"\(\s*(\+/-|\+/−|±|\+|-|−|rac)\s*\)|(?<![A-Za-z])(rac)-", re.IGNORECASE)
_SIGN_WORD = {"+": "plus", "-": "minus", "−": "minus", "±": "rac", "+/-": "rac", "+/−": "rac",
              "rac": "rac"}


def stereo_key(name: str) -> str:
    """normalize_key, but (+), (−), (±) and rac survive as words at the end of the key, wherever
    they stood in the name: '(-)-Blebbistatin' and 'blebbistatin-(-)' -> 'blebbistatin|minus',
    while 'Blebbistatin' -> 'blebbistatin'. R/S and D/L already survive normalize_key as letters."""
    if not name:
        return ""
    words = {_SIGN_WORD[(m.group(1) or m.group(2)).lower()] for m in _SIGN_RE.finditer(name)}
    base = normalize_key(_SIGN_RE.sub(" ", name))
    return base + ("|" + "".join(sorted(words)) if words else "")


# Generic compound patterns — names like "Compound 9" cause false matches
# when they appear as parenthetical aliases in the DB map.
_GENERIC_COMPOUND_RE = re.compile(
    r'^(compound|molecule|inhibitor|analog|derivative|analogue|peptide|fragment|ligand)[\s\-]+\w',
    re.IGNORECASE,
)
_STANDALONE_GENERIC = {
    normalize_key(w) for w in [
        "compound", "peptide", "molecule", "inhibitor", "analog",
        "derivative", "analogue", "fragment", "ligand", "substrate",
    ]
}

def _is_generic_name(name: str) -> bool:
    """Return True if a drug name is a generic compound label that should be
    excluded from the DB lookup map to prevent false matches.

    Catches:
    - Generic compound + identifier: "Compound 9", "Peptide 17"
    - Standalone generic words: "compound", "peptide"
    - Plain numbers: "42", "796"
    """
    name = name.strip()
    if not name:
        return False
    norm = normalize_key(name)
    if not norm:
        return False
    # Pattern 1: Generic compound + identifier ("Compound 9", "Peptide 17")
    if _GENERIC_COMPOUND_RE.match(name):
        return True
    # Pattern 2: Standalone generic word ("compound", "peptide")
    if norm in _STANDALONE_GENERIC:
        return True
    # Pattern 3: Plain numbers ("42", "796")
    if norm.isdigit():
        return True
    return False

# Salt form / formulation suffixes for stripping (order matters — longer forms first)
_SALT_SUFFIXES = [
    # Acid salts
    "dihydrochloride", "trihydrochloride", "hydrochloride", "hydrobromide",
    "hydroiodide", "dihydrobromide", "monohydrochloride",
    # Simple salts
    "hcl", "hbr",
    # Organic acid salts
    "mesylate", "mesilate", "besylate", "besilate", "tosylate", "camsylate",
    "esylate", "maleate", "fumarate", "succinate", "tartrate", "citrate",
    "acetate", "benzoate", "lactate", "oxalate", "sulfonate", "gluconate",
    "propionate", "butyrate", "valerate",
    # Inorganic salts
    "phosphate", "sulfate", "sulphate", "nitrate", "carbonate", "bicarbonate",
    "chloride", "bromide", "iodide", "hemisulfate",
    # Metal salts
    "sodium", "potassium", "calcium", "magnesium", "zinc", "disodium",
    "dipotassium", "trisodium",
    # Hydrates
    "trihydrate", "dihydrate", "monohydrate", "hemihydrate", "hydrate",
    "sesquihydrate", "heptahydrate",
    # Esters / derivatives
    "hemisuccinate", "hemifumarate",
    "diacetate", "triacetate",
    # Common formulation terms
    "trifluoroacetate", "tfa", "sodium salt", "free base",
]

# Derive _SALT_FORMS from _SALT_SUFFIXES for parenthetical filtering
_SALT_FORMS = frozenset(normalize_key(s) for s in _SALT_SUFFIXES)

def strip_salt_suffix(name: str) -> str:
    """
    Iteratively strip salt/form suffixes from a drug name.
    Returns the base name, or the original name if no suffix matched
    or if the base would be too short (< 3 normalized chars).

    Suffixes must be preceded by a space or hyphen (separator-required safety).
    Handles numeric/multiplier prefixes like '2HCl', 'dihydrochloride'.
    """
    if not name:
        return name

    original = name
    # Normalize hyphens between alphabetic chars to spaces for suffix matching
    working = re.sub(r'(?<=[a-zA-Z])-(?=[a-zA-Z])', ' ', name)

    changed = True
    while changed:
        changed = False
        w_lower = working.lower().rstrip()
        for suffix in _SALT_SUFFIXES:
            # Match plain suffix and with numeric/multiplier prefixes
            patterns = [" " + suffix]
            for prefix in ("2", "3", "4", "di", "tri", "mono", "semi", "hemi"):
                patterns.append(f" {prefix}{suffix}")
            for pattern in patterns:
                if w_lower.endswith(pattern):
                    idx = len(working) - len(pattern)
                    working = working[:idx].rstrip()
                    changed = True
                    break
            if changed:
                break

    # If nothing was stripped, return original
    if normalize_key(working) == normalize_key(original):
        return original

    # Safety: base must be >= 3 normalized chars
    if len(normalize_key(working)) < 3:
        return original

    return working

def get_analysis_fuzzy(drug_name: str):
    """
    Attempts to find a report matching the drug name, ignoring case and special chars.
    e.g. 'ots-964' finds 'OTS964'
    """
    if not drug_name: return None
    
    Session = init_db()
    session = Session()
    
    # First try exact match (fast)
    exact = session.query(AnalysisResult).filter_by(drug_name=drug_name).first()
    if exact:
        session.close()
        return _result_to_dict(exact)
    
    # Fallback: Optimized Fuzzy Search
    # 1. Fetch only drug names (Lightweight) instead of full records (Heavy)
    all_names = session.query(AnalysisResult.drug_name).all()
    
    target_norm = normalize_key(drug_name)
    matched_name = None
    
    # 2. Find match in memory (fast on strings)
    for (name,) in all_names:
        if normalize_key(name) == target_norm:
            matched_name = name
            break
            
    # 3. Fetch full record only if match found
    if matched_name:
        match = session.query(AnalysisResult).filter_by(drug_name=matched_name).first()
        session.close()
        return _result_to_dict(match)
        
    session.close()
    return None


def delete_analysis(drug_name: str) -> bool:
    """Deletes an analysis record by drug name."""
    Session = init_db()
    session = Session()
    try:
        # Delete children first to avoid FK issues
        delete_verified_papers(session, drug_name)
        session.query(DrugTarget).filter_by(drug_name=drug_name).delete()
        # Delete the parent record last
        deleted = session.query(AnalysisResult).filter_by(drug_name=drug_name).delete()
        session.commit()
        return deleted > 0
    except Exception as e:
        session.rollback()
        print(f"Error deleting {drug_name}: {e}")
        return False
    finally:
        session.close()

def _result_to_dict(result):
    """Helper to serialize AnalysisResult to dict."""
    return {
        "drug": result.drug_name,
        "report": result.pi_report,
        "genetics_report": result.genetics_report,
        "biochem_report": result.biochem_report,
        "biophysics_report": result.biophysics_report,
        "raw_context": result.raw_context,
        "context": result.system_context,
        "usage": result.usage_stats or {},
        "cost": result.cost or 0.0,
        "time": result.time or 0.0,
        "timestamp": result.timestamp,
        "structured_summary": result.structured_summary or {},
        "mechanism": result.mechanism,
        "reviewer_reports": result.reviewer_reports or {},
        # Legacy compatibility keys removed
        "citation_score": result.citation_score,
        "citation_stats": result.citation_stats or {},
        "corrected_pi_report": result.corrected_pi_report
    }
