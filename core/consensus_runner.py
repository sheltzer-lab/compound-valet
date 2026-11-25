"""Run one drug through first-to-N consensus (`simple_batch.py --consensus`).

Each run is a full, isolated pipeline execution: its DB scratch (verified papers,
identity, everything) is routed to a throwaway local SQLite via `core.scratch_db`,
so parallel runs of the same drug never collide and the target DB only
ever receives the promoted winner.

Resumable at the RUN level: every completed run is persisted to `work_dir/run_NN.json`
the moment it finishes, so a crash mid-drug reuses the finished runs and only re-runs
the missing ones. (Drug-level resume — skipping fully-promoted drugs — lives in the
caller.)

Depends only on `core`; the agreement rule and canonical selector come from
`core.consensus`.
"""
import csv
import json
import logging
import os
import threading
import time

from core.consensus import fields, pick_canonical, decide_consensus, verified_fallback

logger = logging.getLogger(__name__)
_timing_lock = threading.Lock()


def _append_timing(work_dir, drug_name, run_index, status, wall_s, result, n_papers):
    """Per-run throughput telemetry -> <work_root>/run_timings.csv (survives the per-drug
    work-dir cleanup, since it sits at the root). Splits phase-1 (retrieval + citation
    verification, partly API/rate-bound) from the rest (LLM analysis) so we can see which
    stage caps throughput before scaling."""
    pipeline_s = (result or {}).get("time")
    phase1_s = (result or {}).get("phase1_seconds")
    phase2_s = (round(pipeline_s - phase1_s, 1)
                if pipeline_s is not None and phase1_s is not None else None)
    o = _outcome_from_result(result) if result else {}   # per-run outcome for flip attribution
    path = os.path.join(os.path.dirname(os.path.abspath(work_dir)), "run_timings.csv")
    try:
        with _timing_lock:
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["drug", "run_index", "status", "wall_s", "pipeline_s",
                                "phase1_s", "phase2_s", "n_papers",
                                "verdict", "target", "selectivity"])
                w.writerow([drug_name, run_index, status, round(wall_s, 1),
                            pipeline_s, phase1_s, phase2_s, n_papers,
                            o.get("verdict"), o.get("target"), o.get("selectivity_grade")])
    except Exception:
        pass   # telemetry must never break a run


def _outcome_from_result(result: dict) -> dict:
    """Map a pipeline result into the outcome shape `core.consensus.fields` reads."""
    result = result or {}
    ss = result.get("structured_summary") or {}
    if isinstance(ss, list) and ss and isinstance(ss[0], dict):
        ss = ss[0]
    if not isinstance(ss, dict):
        ss = {}
    genes = result.get("target_gene_names_list") or ss.get("target_gene_names") or []
    if isinstance(genes, str):
        genes = [genes]
    return {
        "verdict": ss.get("final_verdict") or ss.get("verdict") or result.get("verdict"),
        "target": result.get("target") or ss.get("target"),
        "target_gene_names": list(genes),
        "selectivity_grade": result.get("selectivity_grade") or ss.get("selectivity_grade"),
    }


def _record(run_index: int, result: dict, papers: list, identity: dict = None) -> dict:
    """Assemble a run record: the raw result + papers + the identity this run resolved,
    plus the derived agreement fields and paper count used by decide_consensus /
    pick_canonical. ``identity`` is the {drug_synonyms, synonym_index} the run wrote to its
    scratch DB (captured by _execute_one_run) so the winner can be promoted with its OWN
    resolved identity instead of a fresh re-resolution."""
    unit_shaped = {"phase2_analysis": {"outcome": _outcome_from_result(result),
                                       "final_report": result.get("report") or ""}}
    return {
        "run_index": run_index,
        "result": result,
        "papers": papers,
        "identity": identity,
        "n_final_papers": len(papers),
        "fields": fields(unit_shaped),
    }


def _run_json(work_dir: str, i: int) -> str:
    return os.path.join(work_dir, f"run_{i:02d}.json")


def _load_run(work_dir: str, i: int):
    """Return a persisted run record for resume, or None if absent/unreadable."""
    path = _run_json(work_dir, i)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        return _record(i, saved["result"], saved["papers"], saved.get("identity"))
    except Exception:
        return None


def _save_run(work_dir: str, rec: dict) -> None:
    """Persist a completed run (result + papers) durably for run-level resume."""
    path = _run_json(work_dir, rec["run_index"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"run_index": rec["run_index"], "result": rec["result"],
                   "papers": rec["papers"], "identity": rec.get("identity")},
                  f, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _execute_one_run(drug_name: str, run_index: int, model_name: str, work_dir: str,
                     log) -> "dict | None":
    """One isolated pipeline execution → persisted run record, or None on failure."""
    from core.pipeline import run_analysis_pipeline
    from core.db import get_verified_papers, get_identity_rows
    from core.scratch_db import scratch_db, dispose_scratch_db

    scratch_path = os.path.join(work_dir, f"run_{run_index:02d}.scratch.db")
    if os.path.exists(scratch_path):
        try:
            os.remove(scratch_path)     # never resume from a half-written scratch
        except OSError:
            pass
    scratch_url = f"sqlite:///{scratch_path}"
    t0 = time.time()
    try:
        with scratch_db(scratch_url):
            result = run_analysis_pipeline(drug_name, model_name=model_name, auto_save=False)
            papers = get_verified_papers(drug_name)   # from the isolated scratch DB
            # The pipeline resolves identity into the scratch DB (write=True: no job_id is
            # passed); capture it here so the winner is promoted with its OWN resolved
            # identity — no fresh re-resolution at promote (which re-invokes the LLM).
            identity = get_identity_rows(drug_name)
    except Exception as e:
        log(f"  {drug_name} run {run_index}: ERROR {e!r}")
        result = None
    finally:
        dispose_scratch_db(scratch_url)
        if os.path.exists(scratch_path):
            try:
                os.remove(scratch_path)   # scratch is ephemeral; the run.json is the artifact
            except OSError:
                pass
    wall_s = time.time() - t0
    n_papers = len(papers) if result is not None else 0
    _append_timing(work_dir, drug_name, run_index,
                   "ok" if result is not None else "error", wall_s, result, n_papers)
    if result is None:
        return None
    rec = _record(run_index, result, papers, identity)
    _save_run(work_dir, rec)
    return rec


def _result(status, con, winner, runs, n_runs) -> dict:
    return {
        "status": status,
        "consensus": con,
        "winner": (winner["result"] if winner else None),
        "winner_papers": (winner["papers"] if winner else []),
        "winner_identity": (winner.get("identity") if winner else None),
        "winner_run_index": (winner["run_index"] if winner else None),
        "runs": [{"run_index": r["run_index"], "n_final_papers": r["n_final_papers"],
                  "verdict": r["fields"]["verdict"], "target": r["fields"]["target"],
                  "selectivity": r["fields"]["selectivity"]} for r in runs],
        "n_runs": n_runs,
    }


def promote_winner(drug_name: str, result: dict, papers: list, identity: dict = None) -> None:
    """Write the winning run to the TARGET DB — the only DB write a
    consensus'd drug makes. Papers first, then the analysis row, then identity; the caller
    checkpoints only after this returns, so a crash mid-promote leaves the drug
    un-checkpointed and cleanly re-done."""
    from core.db import promote_job_papers, save_identity_rows
    # One transaction for papers + report. These used to be two separate commits
    # (save_papers_standalone then save_analysis); a crash between them left the
    # drug with the winner's papers and its previous report.
    promote_job_papers(drug_name, result, papers=papers)
    # Identity: a normal pipeline run resolves drug_synonyms/synonym_index into its scratch DB;
    # those rows were captured into the run record (get_identity_rows) and are upserted here as
    # captured, by the rules in db._write_identity (a resolved identity is never replaced by a
    # non-resolved one; hand-added and merged synonyms stay). We deliberately do NOT re-resolve
    # at promote — a fresh resolve re-invokes the LLM (fragile: timeouts / unparseable output
    # silently drop identity) and reads the winner's FINAL report instead of the draft the run
    # resolved from, so it could disagree with the report's own compound_identity stamp.
    # save_identity_rows is a no-op if the run resolved nothing. Best-effort — never blocks
    # the promotion.
    try:
        save_identity_rows(drug_name, identity)
    except Exception as e:
        logger.warning("Identity persist skipped for %s during promotion: %r", drug_name, e)


def run_batch_consensus(drugs, *, model_name, consensus_n, max_runs, run_pool,
                        work_dir_for, on_result, should_stop=None, log=None):
    """First-to-N consensus across a WHOLE batch, scheduled so the shared ``run_pool`` stays
    full. No drug is pinned to a
    thread — drugs are just state, and whenever a run slot frees the scheduler advances an
    in-flight drug that still needs a run, or admits the next drug from the backlog. So a
    drug in its one-run-at-a-time tail feeds a single slot without stranding the rest.

    ``on_result(drug, res)`` is invoked exactly once per drug (consensus or no_consensus),
    on a serial side thread, so its promote/checkpoint can never stall scheduling. ``res`` is
    {status, consensus, winner (result dict|None), winner_papers, runs summary, n_runs}. ``work_dir_for(drug)`` -> that drug's work
    dir (run-level resume reuses run_NN.json inside it). ``should_stop()`` truthy -> stop
    admitting/submitting and drain the in-flight runs (partial drugs stay un-finalized, so a
    restart re-does only them). Returns ``{"completed": n, "failed": n}``.
    """
    import concurrent.futures as _cf
    log = log or (lambda m: logger.info(m))
    should_stop = should_stop or (lambda: False)
    cap = getattr(run_pool, "_max_workers", 32)

    backlog = list(drugs)
    states = {}          # drug -> {"runs", "next_i", "in_flight", "work_dir"}
    caches = {}          # drug -> pair-agreement memo (decide_consensus / find_consensus)
    in_flight = {}       # future -> (drug, run_index)
    counts = {"completed": 0, "failed": 0}
    promote_pool = _cf.ThreadPoolExecutor(max_workers=1)   # serial promotes, off the scheduler
    promote_futs = []

    def _new_state(drug):
        wd = work_dir_for(drug)
        os.makedirs(wd, exist_ok=True)
        st = {"runs": [], "next_i": 1, "in_flight": 0, "work_dir": wd}
        # Run-level resume: reuse every saved run_NN.json. A failed run leaves a gap (no
        # file); the runs after it are still reused, and new runs number on from the last.
        saved = sorted(int(f[4:-5]) for f in os.listdir(wd)
                       if f.startswith("run_") and f.endswith(".json") and f[4:-5].isdigit())
        for i in saved:
            cached = _load_run(wd, i)
            if cached is not None:
                st["runs"].append(cached)
        if saved:
            st["next_i"] = saved[-1] + 1
        return st

    def _decision(drug, st):
        return decide_consensus(
            drug, [{"run_index": r["run_index"], "fields": r["fields"]} for r in st["runs"]],
            consensus_n, caches.setdefault(drug, {}))

    def _wants(drug, st):
        """How many NEW runs to submit for this drug now (respects in-flight + max-runs)."""
        budget = max_runs - (st["next_i"] - 1)
        if budget <= 0:
            return 0
        have, inflight = len(st["runs"]), st["in_flight"]
        if have + inflight < consensus_n:
            return min(consensus_n - have - inflight, budget)   # initial fill to n
        if _decision(drug, st)["state"] in ("verified", "unverified"):
            return 0                                            # verdict decided + pinned
        return 1 if inflight == 0 else 0                        # pending / verified_locked: one at a time

    def _terminal(drug, st):
        if _decision(drug, st)["state"] in ("verified", "unverified"):
            return True
        return st["in_flight"] == 0 and _wants(drug, st) == 0    # budget spent -> fallback / no_consensus

    def _finalize(drug):
        st = states.pop(drug)
        dec = _decision(drug, st)
        if dec["state"] in ("verified", "unverified"):
            con = dec["con"]
            winner = pick_canonical([r for r in st["runs"] if r["run_index"] in con["members"]])
            res = _result("consensus", con, winner, st["runs"], st["next_i"] - 1)
        else:
            fb = verified_fallback(st["runs"], consensus_n) if dec["state"] == "verified_locked" else None
            if fb:
                res = _result("consensus_fallback", fb["con"], fb["winner"], st["runs"], st["next_i"] - 1)
            else:
                winner = pick_canonical(st["runs"]) if st["runs"] else None
                res = _result("no_consensus", None, winner, st["runs"], st["next_i"] - 1)
        counts["completed" if res["winner"] is not None else "failed"] += 1
        promote_futs.append(promote_pool.submit(on_result, drug, res))

    def _fill():
        while len(in_flight) < cap and not should_stop():
            # advance an active drug that still wants a run (most-progressed first -> finish it),
            # else admit the next drug from the backlog to fill the slack.
            drug = max((d for d, s in states.items() if _wants(d, s) > 0),
                       key=lambda d: len(states[d]["runs"]), default=None)
            if drug is None:
                if not backlog:
                    break
                drug = backlog.pop(0)
                states[drug] = _new_state(drug)
                if _terminal(drug, states[drug]):     # already resolved from resumed runs
                    _finalize(drug)
                    continue
            st = states[drug]
            i = st["next_i"]
            st["next_i"] += 1
            st["in_flight"] += 1
            in_flight[run_pool.submit(_execute_one_run, drug, i, model_name, st["work_dir"], log)] = (drug, i)

    log(f"  batch consensus: {len(backlog)} drugs, pool={cap}, n={consensus_n}, max_runs={max_runs}")
    _fill()
    while in_flight:
        done, _ = _cf.wait(list(in_flight), return_when=_cf.FIRST_COMPLETED)
        for fut in done:
            drug, i = in_flight.pop(fut)
            st = states.get(drug)
            try:
                rec = fut.result()
            except Exception as e:                     # _execute_one_run shouldn't raise, but guard
                log(f"  {drug} run {i}: scheduler caught {e!r}")
                rec = None
            if st is None:
                continue
            st["in_flight"] -= 1
            if rec is not None:
                st["runs"].append(rec)
            if _terminal(drug, st):
                _finalize(drug)
        _fill()

    for f in promote_futs:                             # surface promote/checkpoint errors, don't lose them
        try:
            f.result()
        except Exception as e:
            logger.error("promote/checkpoint failed: %r", e)
    promote_pool.shutdown(wait=True)
    return counts
