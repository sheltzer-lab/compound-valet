"""Report-aware PubChem identity resolver.

Given a drug name and the PI report's **Verification Conclusion**, resolve the name to
the specific PubChem compound the report is actually about — disambiguating research-code
collisions (e.g. "C1632" → a LIN28 inhibitor, NOT cefotaxime) by using the report's
stated target / mechanism. Then gather every transitive salt-union synonym, curate them
skeptically (selection-only, drop misidentifications), hydrate a structure-anchored
identity via RDKit, and upsert it into ``drug_synonyms`` (+ ``synonym_index``).

Fully offline: reads the prebuilt index via ``core.pubchem_offline`` and never calls PubChem at
request time. The store step also canonicalizes the SMILES the index already provided (RDKit,
in-process, in the same transaction) — no network call anywhere in this file.

Flow (``resolve_identity_from_report``):
  1. candidates  – every CID carrying the name, grouped into distinct molecules by parent
  2. pick        – Gemini chooses the molecule matching the report (or none) -> status
  3. salt union  – every transitive synonym of the chosen molecule
  4. filter      – Gemini keeps only true aliases of THIS structure (hallucination-proof)
  5. hydrate     – InChIKey / parent / SMILES / formula (RDKit) + CAS/UNII/ChEMBL xrefs
  6. store       – upsert drug_synonyms + re-emit synonym_index rows, and canonicalize
                   the SMILES (RDKit)

Step 6 runs only with ``write=True``. Every
write keeps hand-added and merged-duplicate synonyms and never replaces a resolved identity
with a non-resolved one (``core.db.KEPT_SYNONYM_SOURCES``, ``keeps_resolved_identity``).
"""

import json
import logging
import os
import re

from core import pubchem_offline
from core.db import (DrugSynonym, identity_row, init_db, keeps_resolved_identity,
                     normalize_key, replace_synonym_index)
from core.llm import generate_with_search
from core.smiles_canonicalize import canonicalize, canonicalize_parent
from core.usage import sum_usage

logger = logging.getLogger(__name__)

PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
MAX_CANDIDATE_GROUPS = 25       # distinct molecules shown to the picker
MAX_SYNS_PER_CANDIDATE = 15     # representative synonyms per candidate block
MAX_SYNS_TO_FILTER = 400        # cap the salt-union list handed to the filter


class ResolutionError(Exception):
    """Raised for hard failures (no index, LLM unparseable). A *non-match*
    (no candidate fits, biologic, combination) is NOT an error — it is returned
    as a result with ``status`` in {unresolved, out_of_scope, combination}."""


# --------------------------------------------------------------------------- prompts
def _load_prompt(name):
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- RDKit
def _rdkit():
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem import rdMolDescriptors
        from rdkit.Chem.MolStandardize import rdMolStandardize
        RDLogger.DisableLog("rdApp.*")
        return Chem, rdMolDescriptors, rdMolStandardize
    except ImportError:
        return None, None, None


def _inchikey(smiles, desalt=False):
    """InChIKey of a SMILES. ``desalt`` first strips to the largest organic fragment
    (RDKit ``FragmentParent``) to match our parent-anchored identity."""
    Chem, _, rdMolStandardize = _rdkit()
    if Chem is None or not smiles:
        return None
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        if desalt:
            try:
                mol = rdMolStandardize.FragmentParent(mol)
            except Exception:
                pass
        return Chem.MolToInchiKey(mol)
    except Exception:
        return None


def _inchi(smiles):
    Chem, _, _ = _rdkit()
    if Chem is None or not smiles:
        return None
    try:
        mol = Chem.MolFromSmiles(smiles)
        return Chem.MolToInchi(mol) if mol else None
    except Exception:
        return None


def _formula(smiles):
    Chem, rdMolDescriptors, _ = _rdkit()
    if Chem is None or not smiles:
        return None
    try:
        mol = Chem.MolFromSmiles(smiles)
        return rdMolDescriptors.CalcMolFormula(mol) if mol else None
    except Exception:
        return None


# --------------------------------------------------------------------------- cross-refs
_XREF_PATTERNS = {
    "cas": re.compile(r"^\d{2,7}-\d{2}-\d$"),
    "unii": re.compile(r"^[A-Z0-9]{10}$"),
    "chembl_id": re.compile(r"^CHEMBL\d+$", re.I),
    "drugbank_id": re.compile(r"^DB\d{5}$", re.I),
    "chebi_id": re.compile(r"^CHEBI:\d+$", re.I),
}


def _extract_xrefs(synonyms):
    """Pull registry identifiers straight out of the synonym list (PubChem lists
    CAS / UNII / ChEMBL / DrugBank / ChEBI as synonyms — no network needed)."""
    out = {}
    for s in synonyms:
        t = s.strip()
        for key, pat in _XREF_PATTERNS.items():
            if key not in out and pat.match(t):
                out[key] = t.upper() if key in ("chembl_id", "drugbank_id", "chebi_id") else t
    return out


# --------------------------------------------------------------------------- report
def _get(report, *keys):
    for k in keys:
        if isinstance(report, dict):
            if report.get(k):
                return report[k]
        elif getattr(report, k, None):
            return getattr(report, k)
    return None


def _conclusion_text(report):
    """Build the disambiguating context from the report: the markdown 'Verification
    Conclusion' section if present, else the structured_summary fields. ``report`` may
    be a dict, an AnalysisResult-like object, or a plain conclusion string."""
    if isinstance(report, str):
        return report.strip()
    parts = []
    md = _get(report, "corrected_pi_report", "pi_report")
    if md:
        # header is e.g. "## 1. Verification Conclusion" — allow the section number
        m = re.search(r"(?im)^#{1,6}\s*(?:\d+[.)]\s*)?Verification Conclusion\s*$", md)
        if m:
            rest = md[m.end():]
            nxt = re.search(r"(?m)^#{1,6}\s+\S", rest)  # up to the next header
            parts.append(rest[: nxt.start()].strip() if nxt else rest.strip())
    ss = _get(report, "structured_summary")
    if isinstance(ss, str):
        try:
            ss = json.loads(ss)
        except Exception:
            ss = None
    if isinstance(ss, dict):
        fields = [
            ("Verdict", ss.get("final_verdict")),
            ("Target", ss.get("target")),
            ("Target gene names", ", ".join(ss.get("target_gene_names") or []) or None),
            ("Selectivity", ss.get("selectivity_grade")),
            ("Primary off-target", ss.get("primary_off_target")),
        ]
        struct = "\n".join(f"- **{k}**: {v}" for k, v in fields if v)
        if struct and not parts:
            parts.append(struct)
        elif struct:
            parts.append("Structured fields:\n" + struct)
    if not parts:
        v = _get(report, "verdict")
        parts.append(f"Verdict: {v}" if v else "(no conclusion text available)")
    return "\n\n".join(parts).strip()


# --------------------------------------------------------------------------- candidates
def _candidate_groups(index, cids):
    """Group candidate CIDs into distinct molecules by their salt-union parent, so the
    picker chooses a *compound*, not a salt form."""
    groups = {}
    for c in cids:
        groups.setdefault(index.parent_cid(c), set()).add(int(c))
    out = []
    for parent, members in groups.items():
        smi = index.smiles_for_cid(parent)
        if not smi:
            smi = next((index.smiles_for_cid(m) for m in members if index.smiles_for_cid(m)), None)
        syns = index.synonyms_for_cids(members)
        out.append({
            "parent": parent,
            "members": sorted(members),
            "smiles": smi,
            "formula": _formula(smi),
            "synonyms": syns,
        })
    out.sort(key=lambda g: len(g["synonyms"]), reverse=True)  # the real drug is usually the busiest
    return out[:MAX_CANDIDATE_GROUPS]


def _format_candidates(groups):
    lines = []
    for i, g in enumerate(groups):
        syns = "; ".join(g["synonyms"][:MAX_SYNS_PER_CANDIDATE]) or "(none)"
        lines.append(
            f"[{i}] CID {g['parent']} | formula {g['formula'] or '?'} | SMILES {g['smiles'] or '?'}\n"
            f"    synonyms: {syns}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- LLM helpers
def _extract_json(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


def _gen(prompt, model, use_search=True):
    resp = generate_with_search(
        prompt, model_name=model, generation_config_overrides={"temperature": 0.0},
        use_search=use_search,
    )
    if resp.get("error"):
        raise ResolutionError(f"LLM call failed: {resp.get('text')}")
    return resp.get("text", ""), resp.get("usage", {})


def _pick(drug_name, conclusion, groups, model, use_search=True):
    prompt = _load_prompt("identity_candidate_pick.md").format(
        drug_name=drug_name,
        verification_conclusion=conclusion,
        candidates=_format_candidates(groups),
    )
    text, usage = _gen(prompt, model, use_search)
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ResolutionError(f"candidate-pick returned unparseable output: {text[:300]!r}")
    choice = data.get("choice")
    status = data.get("status", "unresolved")
    if isinstance(choice, int) and 0 <= choice < len(groups) and status == "resolved":
        return groups[choice], data, usage
    return None, data, usage


def _filter_synonyms(identity, target, synonyms, model, use_search=True):
    allowed = list(dict.fromkeys(synonyms))[:MAX_SYNS_TO_FILTER]  # dedupe, cap
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(allowed))
    prompt = _load_prompt("identity_synonym_filter.md").format(
        preferred_name=identity.get("preferred_name") or "?",
        formula=identity.get("formula") or "?",
        smiles=identity.get("smiles") or "?",
        cid=identity.get("cid") or "?",
        target=target or "?",
        synonyms=numbered,
    )
    text, usage = _gen(prompt, model, use_search)
    data = _extract_json(text)
    if not isinstance(data, list):
        # filter failed — fall back to the unfiltered set rather than losing everything
        return allowed, usage
    allowed_set = set(allowed)
    kept = [s for s in data if isinstance(s, str) and s in allowed_set]  # selection-only
    return (kept or allowed), usage


# --------------------------------------------------------------------------- storage
# Statuses with no single resolvable small-molecule structure (biologics, peptides >15aa,
# multi-drug combinations); SMILES canonicalization is skipped for these.
_SKIP_SMILES_STATUSES = {"out_of_scope", "combination"}


def _index_rows(drug_name, synonyms, parent_inchikey):
    """synonym_index rows for a resolved drug's curated synonyms, one per normalized alias."""
    rows, seen = [], set()
    for syn in synonyms:
        key = normalize_key(syn)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({"alias_norm": key, "alias_raw": syn, "canonical_drug_name": drug_name,
                     "parent_inchikey": parent_inchikey,
                     "source": "pubchem:report-resolved", "confidence": "high"})
    return rows


def _store(session, drug_name, row_fields, synonyms):
    """Upsert the resolved identity and canonicalize its SMILES, all in one transaction.

    Called by resolve_identity_from_report with write=True (batch and consensus runs of the
    analysis pipeline)."""
    row = identity_row(session, drug_name)
    if row is None:
        row = DrugSynonym(drug_name=drug_name)
        session.add(row)
    for k, v in row_fields.items():
        setattr(row, k, v)

    # Canonicalize the SMILES the resolver already read from the offline index -- inline on THIS
    # session (pure-CPU RDKit, no network, no second session), so it cannot contend with the
    # caller's session in a parallel/batch store loop. Fill only when we have a SMILES and the row
    # is a single resolvable structure; index-miss / biologic / combination rows stay unfilled
    # (expected). Recomputed on every store, so a re-resolution to a new
    # structure does not keep the old one's canonical SMILES. Own try/except so a bad SMILES can
    # never break the identity write.
    if row.smiles and row.status not in _SKIP_SMILES_STATUSES:
        try:
            row.canonicalized_smiles = canonicalize(row.smiles)
            row.canonicalized_parent_smiles = canonicalize_parent(row.smiles)
        except Exception as e:
            logger.warning("SMILES canonicalization skipped for %s: %s", drug_name, e)

    # re-emit synonym_index rows for this drug, keeping the hand-added and merged ones
    replace_synonym_index(session, {drug_name, row.drug_name},
                          _index_rows(drug_name, synonyms, row_fields.get("parent_inchikey")))
    session.commit()


# --------------------------------------------------------------------------- main
def resolve_identity_from_report(drug_name, report, *, index=None, model=None,
                                 write=True, session=None, use_search=True, extra_names=None):
    """Resolve ``drug_name`` to a structure-anchored identity using ``report``'s
    Verification Conclusion. Returns a result dict::

        {"drug_name", "status", "identity": {...} | None, "synonyms": [...], "pick": {...}}

    ``status`` is one of resolved / unresolved / out_of_scope / combination. On
    ``status != "resolved"`` ``identity`` is None. Raises ResolutionError only on hard
    failures (missing index, unparseable LLM output)."""
    index = index or pubchem_offline.get_index()
    conclusion = _conclusion_text(report)
    usage = {}  # accumulated LLM usage (tokens + search_grounding_queries) across calls

    cids, _seen = [], set()  # union candidates over the name + any extra_names (e.g. parenthetical codes)
    for _nm in [drug_name, *(extra_names or [])]:
        for _c in index.candidates_for_name(_nm):
            if _c not in _seen:
                _seen.add(_c)
                cids.append(_c)
    if not cids:
        result = {"drug_name": drug_name, "status": "unresolved", "identity": None,
                  "synonyms": [], "pick": {"reason": "no PubChem candidates for this name"},
                  "usage": usage}
        if write:
            _persist_unresolved(session, drug_name, "unresolved", "report:no-candidates")
        return result

    groups = _candidate_groups(index, cids)
    chosen, pick, pick_usage = _pick(drug_name, conclusion, groups, model, use_search)
    usage = sum_usage(usage, pick_usage)
    if chosen is None:
        status = pick.get("status", "unresolved")
        if status not in ("unresolved", "out_of_scope", "combination"):
            status = "unresolved"
        resolved_via = "report:" + (pick.get("out_of_scope_reason") or status)
        if write:
            _persist_unresolved(session, drug_name, status, resolved_via)
        return {"drug_name": drug_name, "status": status, "identity": None,
                "synonyms": [], "pick": pick, "usage": usage}

    # chosen molecule
    parent = chosen["parent"]
    rep_cid = next((m for m in chosen["members"] if index.smiles_for_cid(m)), parent)
    smiles = index.smiles_for_cid(rep_cid)
    parent_smiles = index.smiles_for_cid(parent) or smiles

    # full salt-union synonyms, then skeptical filter
    all_syns = index.salt_union_synonyms(parent)
    # Use the report's own name as the preferred label — PubChem's synonym[0] is often a
    # wrong title (e.g. CID 448042's title is "Ximelegatran" for the Y-27632 structure),
    # and seeding the filter prompt with a wrong name would mislead it.
    preferred = drug_name
    identity_stub = {"preferred_name": preferred, "formula": _formula(parent_smiles) or _formula(smiles),
                     "smiles": parent_smiles or smiles, "cid": parent}
    target = _target_hint(report)
    kept_syns, filt_usage = _filter_synonyms(identity_stub, target, all_syns, model, use_search)
    usage = sum_usage(usage, filt_usage)

    xrefs = _extract_xrefs(all_syns)  # registry IDs from the full list, not just kept
    inchikey = _inchikey(smiles)
    parent_inchikey = _inchikey(parent_smiles, desalt=True) or _inchikey(parent_smiles)
    identity = {
        "cid": str(rep_cid),
        "parent_cid": str(parent),
        "smiles": smiles,
        "parent_smiles": parent_smiles,
        "inchikey": inchikey,
        "inchikey_skeleton": inchikey[:14] if inchikey else None,
        "parent_inchikey": parent_inchikey,
        "parent_inchikey_skeleton": parent_inchikey[:14] if parent_inchikey else None,
        "inchi": _inchi(parent_smiles or smiles),
        "formula": _formula(parent_smiles) or _formula(smiles),
        "preferred_name": preferred,
        **xrefs,
    }

    row_fields = {
        "status": "resolved",
        "resolved_via": "report:llm-pick",
        "query_used": drug_name,
        "cid": identity["cid"],
        "parent_cid": identity["parent_cid"],
        "smiles": identity["smiles"],
        "parent_smiles": identity["parent_smiles"],
        "inchikey": identity["inchikey"],
        "inchikey_skeleton": identity["inchikey_skeleton"],
        "parent_inchikey": identity["parent_inchikey"],
        "parent_inchikey_skeleton": identity["parent_inchikey_skeleton"],
        "inchi": identity["inchi"],
        "formula": identity["formula"],
        "cas": xrefs.get("cas"),
        "unii": xrefs.get("unii"),
        "chembl_id": xrefs.get("chembl_id"),
        "drugbank_id": xrefs.get("drugbank_id"),
        "chebi_id": xrefs.get("chebi_id"),
        "n_synonyms": len(kept_syns),
        "sample_synonyms": "; ".join(kept_syns[:6]),
    }
    if write:
        sess = session or init_db()()
        try:
            _store(sess, drug_name, row_fields, kept_syns)
        finally:
            if session is None:
                sess.close()

    return {"drug_name": drug_name, "status": "resolved", "identity": identity,
            "synonyms": kept_syns, "pick": pick, "usage": usage}


def _target_hint(report):
    if isinstance(report, str):
        return None
    ss = _get(report, "structured_summary")
    if isinstance(ss, str):
        try:
            ss = json.loads(ss)
        except Exception:
            ss = None
    if isinstance(ss, dict):
        return ss.get("target") or ", ".join(ss.get("target_gene_names") or []) or None
    return _get(report, "target")


def _persist_unresolved(session, drug_name, status, resolved_via):
    sess = session or init_db()()
    try:
        row = identity_row(sess, drug_name)
        if keeps_resolved_identity(row, status):
            return  # a re-resolution that found nothing leaves a resolved identity alone
        if row is None:
            row = DrugSynonym(drug_name=drug_name)
            sess.add(row)
        row.status = status
        row.resolved_via = resolved_via
        row.query_used = drug_name
        sess.commit()
    finally:
        if session is None:
            sess.close()


def format_for_review(result):
    """Render a resolve result as a compact, reviewer-facing identity block — the
    structure-anchored identity + curated synonyms, for auditing the PI's compound ID
    (misidentification, wrong-compound papers, papers missed under a synonym)."""
    status = result.get("status")
    if status != "resolved" or not result.get("identity"):
        return (f"An independent structure-anchored resolver could NOT confidently resolve "
                f"'{result.get('drug_name')}' to a single PubChem compound (status: {status}). "
                f"Scrutinize the PI's compound identification with extra care.")
    ident, syns = result["identity"], result.get("synonyms", [])
    xrefs = ", ".join(f"{k}={ident[k]}" for k in
                      ("cas", "chembl_id", "drugbank_id", "unii", "chebi_id") if ident.get(k))
    lines = [
        "An independent structure-anchored resolver (PubChem, anchored on the report's own "
        "conclusion) identified the compound as:",
        f"- Preferred name: {ident.get('preferred_name') or result.get('drug_name')}",
        f"- Molecular formula: {ident.get('formula')}",
        f"- Parent InChIKey: {ident.get('parent_inchikey')}  |  PubChem CID: {ident.get('cid')}",
        f"- SMILES: {ident.get('smiles')}",
    ]
    if xrefs:
        lines.append(f"- Cross-refs: {xrefs}")
    if syns:
        lines.append(f"- Known synonyms / search aliases: {', '.join(syns[:30])}")
    reason = (result.get("pick") or {}).get("reason")
    if reason:
        lines.append(f"- Basis: {reason}")
    return "\n".join(lines)
