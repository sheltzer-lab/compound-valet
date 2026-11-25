"""Run-to-run consensus: when do two pipeline runs agree, and which agreeing run
becomes canonical.

This is the home of the agreement rule. The pipeline (`simple_batch` via `core`) uses it,
and it depends only on `core`.

Agreement:
  * verdict, normalized (INCONCLUSIVE/PARTIAL -> UNKNOWN), must match;
  * if BOTH runs are UNVERIFIED, that alone is enough — nothing was determined, so
    target and selectivity are moot;
  * otherwise selectivity must match (an *omitted* grade normalizes to UNKNOWN, since
    the rubric tells the PI to omit it for classes it doesn't apply to), AND target
    must match — two "no specific target" runs agree, a no-target/real-target pair does
    not, and two real targets go to the LLM matcher
    (`core.target_matching.llm_check_target_match`).

The matcher is not guaranteed transitive, so a consensus set must agree on EVERY pair.
"""
import itertools
import re
from typing import Any, Dict, List, Optional, Tuple


# --- normalizers + no-target detection ---------------------------------------------

_NO_TARGET = {"", "none", "unknown", "n/a", "na", "not determined", "not applicable",
              "unverified", "no specific target", "no verified target", "no target",
              "off-target toxicity", "off target toxicity", "nonspecific", "non-specific"}


def _vnorm(v: Optional[str]) -> str:
    v = (v or "").upper().strip()
    return {"INCONCLUSIVE": "UNKNOWN", "PARTIAL": "UNKNOWN"}.get(v, v)


def _snorm(sel: Optional[str]) -> str:
    """An omitted selectivity grade means the same as UNKNOWN: no determination made.

    Normalized here rather than in fields() so recorded data keeps the distinction
    (omitted-by-rubric vs looked-and-found-nothing) while agreement does not depend on
    it. Mirrors how _vnorm folds INCONCLUSIVE/PARTIAL into UNKNOWN.
    """
    return (sel or "").upper().strip() or "UNKNOWN"


def _is_no_target(t: Optional[str], genes) -> bool:
    """No specific target: no real gene symbols + a none/unknown-style label
    (parentheticals ignored, e.g. 'Unknown (Off-target toxicity)')."""
    toks = [g for g in (genes or []) if g and str(g).strip().lower() != "none"]
    if toks:
        return False
    head = re.sub(r"\(.*?\)", "", str(t or "")).strip().lower()
    return " ".join(head.split()) in _NO_TARGET


def _match_with_retry(drug: str, a: Dict[str, Any], b: Dict[str, Any], retries: int = 2):
    """Call the target matcher, retrying on an empty/None response. The matcher
    returns None on truncated/empty output (it doesn't retry that internally). A couple of
    quick retries recover the transient ones; a persistently empty pair stays None without
    burning much time."""
    import time
    from core.target_matching import llm_check_target_match
    for attempt in range(retries + 1):
        m = llm_check_target_match(a["target"], a["gene_str"], b["target"], b["genes"],
                                   drug, drug, b["selectivity"], b["pi"])
        if m is not None:
            return m
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    return None


# --- comparison fields + pairwise agreement ----------------------------------------

def fields(unit: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the comparison fields out of a run record ({"phase2_analysis": {"outcome": ...}}; see core/consensus_runner.py)."""
    p2 = unit.get("phase2_analysis", {}) or {}
    o = p2.get("outcome", {}) or {}
    genes = o.get("target_gene_names") or []
    return {
        "target": o.get("target") or "",
        "genes": genes,
        "gene_str": ",".join(genes) if genes else "",
        "selectivity": (o.get("selectivity_grade") or "").upper().strip(),
        "verdict": _vnorm(o.get("verdict")),
        "pi": p2.get("final_report") or "",
    }


def pair_agrees(drug: str, a: Dict[str, Any], b: Dict[str, Any],
                cache: Optional[Dict] = None,
                key: Optional[Tuple] = None) -> Optional[bool]:
    """True / False, or None when the target matcher failed and we cannot say.

    None is never treated as agreement by callers — an unknown pair must not be able
    to close a consensus set.
    """
    if cache is not None and key is not None and key in cache:
        return cache[key]

    result: Optional[bool]
    if a["verdict"] != b["verdict"]:
        result = False
    elif a["verdict"] == "UNVERIFIED":
        result = True                      # verdict alone suffices
    elif _snorm(a["selectivity"]) != _snorm(b["selectivity"]):
        result = False
    else:
        na, nb = _is_no_target(a["target"], a["genes"]), _is_no_target(b["target"], b["genes"])
        if na and nb:
            result = True                  # reproducibly "no specific target"
        elif na != nb:
            result = False
        else:
            result = _match_with_retry(drug, a, b)   # True / False / None

    if cache is not None and key is not None:
        cache[key] = result
    return result


def find_consensus(drug: str, runs: List[Dict[str, Any]], n: int = 3,
                   cache: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """First set of `n` runs that mutually agree, or None.

    `runs` is a list of {"run_index": int, "fields": {...}} in run order. Combinations
    are enumerated in run order, so the earliest-converging set wins.
    """
    cache = cache if cache is not None else {}
    idx = list(range(len(runs)))
    for combo in itertools.combinations(idx, n):
        ok = True
        for i, j in itertools.combinations(combo, 2):
            key = (runs[i]["run_index"], runs[j]["run_index"])
            if pair_agrees(drug, runs[i]["fields"], runs[j]["fields"], cache, key) is not True:
                ok = False
                break
        if ok:
            members = [runs[i]["run_index"] for i in combo]
            rep = runs[combo[0]]["fields"]        # representative = earliest member
            return {
                "members": members,
                "verdict": rep["verdict"],
                "target": rep["target"],
                "genes": list(rep["genes"]),
                "selectivity": rep["selectivity"],
                "fields": rep,
            }
    return None


# --- canonical selection -----------------------------------------------------------

def pick_canonical(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """First-pass canonical selector: among agreeing runs, the one whose final report
    stands on the most verified papers (`n_final_papers`), ties broken by the earliest
    run. Items need `n_final_papers` and `run_index`. Returns None for an empty list.

    Breadth only — a report with more thin citations beats one with fewer well-analyzed
    ones. Deliberately a v1; a richer scorer replaces this without touching agreement.
    """
    if not runs:
        return None
    return max(runs, key=lambda r: (r.get("n_final_papers") or 0, -(r.get("run_index") or 0)))


# --- symmetric verdict-race consensus -----------------------------------------------
# Replaces the old "first n mutually-agreeing runs" rule, which was ASYMMETRIC: two
# UNVERIFIED runs agreed on verdict alone, but VERIFIED runs also had to match target +
# selectivity — so a VERIFIED plurality could lose the verdict to an easier-to-form
# UNVERIFIED trio. The rule below decides the verdict by a symmetric race, then pins the
# VERIFIED answer with the same strict agreement.

def decide_consensus(drug: str, runs: List[Dict[str, Any]], n: int = 3,
                     cache: Optional[Dict] = None,
                     unlock_at: Optional[int] = None) -> Dict[str, Any]:
    """Two-phase, verdict-symmetric consensus on the runs SO FAR (in run order).

    Phase 1 — verdict race: count VERIFIED vs UNVERIFIED runs; whichever reaches ``n``
    FIRST decides the verdict. ``n`` VERIFIEDs before ``n`` UNVERIFIEDs locks VERIFIED (it
    can no longer be lost to an easier UNVERIFIED cluster), and vice versa.
    Phase 2 — only when VERIFIED wins: keep running until ``n`` VERIFIED runs mutually
    agree on verdict+selectivity+target (``find_consensus``), which pins the answer.
    UNVERIFIED needs no phase 2 — nothing to pin.

    UNLOCK — once ``unlock_at`` runs (default ``n + 1``) have come back UNVERIFIED, they
    take the verdict back even if VERIFIED won the race earlier. Without this a drug can
    be recorded VERIFIED on a minority: Furaneol locked VERIFIED at run 4 of the sequence
    V V U V U U U U U V and finished 4-6 against itself, because the VERIFIED trio formed
    before the UNVERIFIED one and nothing afterwards could reopen it. The unlock is
    checked before phase 2, so it overrides a pinned trio as well as a bare lock.

    ``runs`` items: ``{"run_index": int, "fields": {...}}``. Returns ``{"state", "con"}``:
      * ``"unverified"``      — ``n`` UNVERIFIED reached first, or the unlock fired;
                                ``con`` set, finalize now.
      * ``"verified"``        — VERIFIED locked AND an agreeing trio exists; ``con`` set.
      * ``"verified_locked"`` — VERIFIED locked, no trio yet; ``con`` None (run more / fall back).
      * ``"pending"``         — neither verdict has reached ``n`` yet; run more.
    """
    cache = cache if cache is not None else {}
    unlock_at = n + 1 if unlock_at is None else unlock_at
    vc = uc = 0
    winner = None
    for r in runs:                                  # run order: first verdict to n wins
        vd = r["fields"]["verdict"]
        if vd == "VERIFIED":
            vc += 1
            if vc >= n:
                winner = "VERIFIED"; break
        elif vd == "UNVERIFIED":
            uc += 1
            if uc >= n:
                winner = "UNVERIFIED"; break
    if winner is None:
        return {"state": "pending", "con": None}
    # Unlock: a clear UNVERIFIED majority reclaims a verdict VERIFIED won on order alone.
    if winner == "VERIFIED":
        n_unv = sum(1 for r in runs if r["fields"]["verdict"] == "UNVERIFIED")
        if n_unv >= unlock_at:
            winner = "UNVERIFIED"
    if winner == "UNVERIFIED":
        unv = [r for r in runs if r["fields"]["verdict"] == "UNVERIFIED"]
        rep = unv[0]["fields"]
        return {"state": "unverified",
                "con": {"members": [r["run_index"] for r in unv[:n]], "verdict": "UNVERIFIED",
                        "target": rep["target"], "genes": list(rep["genes"]),
                        "selectivity": rep["selectivity"], "fields": rep, "method": "verdict_race"}}
    # VERIFIED locked — pin it with an agreeing trio among the VERIFIED runs only.
    vruns = [{"run_index": r["run_index"], "fields": r["fields"]}
             for r in runs if r["fields"]["verdict"] == "VERIFIED"]
    con = find_consensus(drug, vruns, n, cache)
    if con:
        con["method"] = "trio"
        return {"state": "verified", "con": con}
    return {"state": "verified_locked", "con": None}


def verified_fallback(runs: List[Dict[str, Any]], n: int = 3) -> Optional[Dict[str, Any]]:
    """VERIFIED is locked but no ``n``-run trio agreed within the run budget. Report the
    MOST COMMON selectivity across the VERIFIED runs, and draw the canonical run from the
    runs carrying that selectivity — so the promoted report's own grade matches the
    reported one. ``runs`` items need "fields", "run_index", "n_final_papers". Returns
    ``{"con", "winner"}`` (winner is a full record from ``runs``), or None if no VERIFIED runs.
    """
    import collections
    vruns = [r for r in runs if r["fields"]["verdict"] == "VERIFIED"]
    if not vruns:
        return None
    modal = collections.Counter(_snorm(r["fields"]["selectivity"]) for r in vruns).most_common(1)[0][0]
    cands = [r for r in vruns if _snorm(r["fields"]["selectivity"]) == modal]
    winner = pick_canonical(cands)
    rep = winner["fields"]
    con = {"members": sorted(r["run_index"] for r in cands), "verdict": "VERIFIED",
           "target": rep["target"], "genes": list(rep["genes"]), "selectivity": rep["selectivity"],
           "fields": rep, "method": "fallback_modal_selectivity"}
    return {"con": con, "winner": winner}
