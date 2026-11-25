"""Tests for core.consensus_runner.run_batch_consensus — the pool-filling batch scheduler.

The real pipeline run (_execute_one_run) is replaced with a fast fake whose outcome is a
pure function of (drug, run_index), so consensus scenarios are deterministic; the target
matcher is mocked to string-equality. We assert both correctness (first-to-N semantics,
resume) and the property the scheduler exists for: the run pool stays saturated.
"""
import threading
import time

import pytest
from concurrent.futures import ThreadPoolExecutor

import core.consensus_runner as cr
from core.consensus_runner import run_batch_consensus

V, U, SEL, UNK = "VERIFIED", "UNVERIFIED", "SELECTIVE", "UNKNOWN"


def _outcome(drug, i):
    """(verdict, target, selectivity, n_papers) as a pure function of (drug, run_index)."""
    if drug.startswith("clean"):
        return [(V, "EGFR", SEL, 8), (V, "EGFR", SEL, 10), (V, "EGFR", SEL, 9)][min(i - 1, 2)]
    if drug.startswith("tail"):   # runs 1,3,4 agree -> consensus at run 4; run 2 dissents
        return [(V, "EGFR", SEL, 5), (U, "", UNK, 3), (V, "EGFR", SEL, 7),
                (V, "EGFR", SEL, 6)][min(i - 1, 3)]
    if drug.startswith("nocon"):  # all VERIFIED, every run a distinct target -> no trio ->
        return (V, f"T{i}", SEL, i)            # verdict locks VERIFIED, modal-selectivity fallback
    if drug.startswith("steal"):  # 3 VERIFIEDs (distinct targets) lock the verdict FIRST;
        return (V, f"S{i}", SEL, i) if i <= 3 else (U, "", UNK, 3)   # later UNVERIFIEDs can't steal it
    return (U, "", UNK, 4)        # "unver": all UNVERIFIED -> UNVERIFIED at 3


@pytest.fixture
def harness(monkeypatch):
    """Install the fake pipeline + string-equality target matcher; track concurrency/calls."""
    state = {"cur": 0, "max": 0, "calls": 0, "lock": threading.Lock()}

    def fake_execute(drug, i, model_name, work_dir, log):
        with state["lock"]:
            state["calls"] += 1
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.03)                        # force overlap so concurrency is observable
        with state["lock"]:
            state["cur"] -= 1
        verdict, target, sel, npapers = _outcome(drug, i)
        result = {"structured_summary": {"final_verdict": verdict, "target": target,
                                         "selectivity_grade": sel,
                                         "target_gene_names": [target] if target else []},
                  "report": f"{drug} run {i}", "target": target, "selectivity_grade": sel}
        rec = cr._record(i, result, [{"doi": f"{drug}-{i}-{k}"} for k in range(npapers)])
        cr._save_run(work_dir, rec)             # persist for run-level resume
        return rec

    monkeypatch.setattr(cr, "_execute_one_run", fake_execute)
    monkeypatch.setattr("core.target_matching.llm_check_target_match",
                        lambda at, ags, bt, bg, da, db, bsel, bpi: at == bt)
    return state


def _run(drugs, tmp_path, cap=4, max_runs=6, on_result=None):
    results = {}
    def default_on_result(drug, res):
        results[drug] = res
    with ThreadPoolExecutor(max_workers=cap) as pool:
        counts = run_batch_consensus(
            drugs, model_name="m", consensus_n=3, max_runs=max_runs, run_pool=pool,
            work_dir_for=lambda d: str(tmp_path / d),
            on_result=on_result or default_on_result, log=lambda m: None)
    return counts, results


def test_scheduler_correctness_and_pool_stays_full(harness, tmp_path):
    drugs = [f"clean{i}" for i in range(4)] + [f"tail{i}" for i in range(3)] \
            + [f"nocon{i}" for i in range(2)] + ["steal0", "unver0"]
    counts, res = _run(drugs, tmp_path, cap=4, max_runs=6)

    # on_result fired exactly once per drug
    assert set(res) == set(drugs)
    assert counts == {"completed": 11, "failed": 0}   # all produced a winner

    # clean: VERIFIED trio at 3, winner = run 2 (most papers, 10)
    for d in [f"clean{i}" for i in range(4)]:
        assert res[d]["status"] == "consensus" and res[d]["n_runs"] == 3
        assert res[d]["winner_run_index"] == 2 and res[d]["consensus"]["verdict"] == V
    # tail: VERIFIED locked at run 4, trio (members 1,3,4), winner = run 3 (7 papers)
    for d in [f"tail{i}" for i in range(3)]:
        assert res[d]["status"] == "consensus" and res[d]["n_runs"] == 4
        assert res[d]["winner_run_index"] == 3
        assert sorted(res[d]["consensus"]["members"]) == [1, 3, 4]
    # nocon: all VERIFIED but targets never agree -> verdict locks VERIFIED, modal-selectivity
    # fallback at max_runs (the OLD asymmetric rule returned no_consensus here).
    for d in [f"nocon{i}" for i in range(2)]:
        assert res[d]["status"] == "consensus_fallback" and res[d]["n_runs"] == 6
        assert res[d]["consensus"]["verdict"] == V
        assert res[d]["winner"] is not None and res[d]["winner_run_index"] == 6
    # steal: 3 VERIFIEDs lock the verdict before the later UNVERIFIEDs can form a trio ->
    # VERIFIED (fallback). The OLD rule would have returned UNVERIFIED. THE key fix.
    assert res["steal0"]["status"] == "consensus_fallback"
    assert res["steal0"]["consensus"]["verdict"] == V and res["steal0"]["n_runs"] == 6
    # unver: 3 UNVERIFIEDs win the verdict race -> UNVERIFIED at 3
    assert res["unver0"]["status"] == "consensus" and res["unver0"]["n_runs"] == 3
    assert res["unver0"]["consensus"]["verdict"] == U

    # THE POINT: the pool filled to cap and never exceeded it.
    assert harness["max"] == 4
    # total runs: clean 4*3 + tail 3*4 + nocon 2*6 + steal 6 + unver 3 = 45
    assert harness["calls"] == 45


def test_pool_stays_full_when_everything_is_in_a_tail(harness, tmp_path):
    # All drugs need a 4th (tail) round; with a per-drug driver, tails would strand slots.
    # The scheduler must still hold all `cap` slots by admitting/advancing across drugs.
    drugs = [f"tail{i}" for i in range(8)]
    counts, res = _run(drugs, tmp_path, cap=4, max_runs=6)
    assert counts["completed"] == 8
    assert all(res[d]["n_runs"] == 4 for d in drugs)
    assert harness["max"] == 4                        # pool never starved by the tails


def test_promote_off_thread_does_not_stall_scheduling(harness, tmp_path):
    # A slow on_result (promote) must not pause the scheduler — it runs on a serial side thread.
    def slow_on_result(drug, res):
        time.sleep(0.05)
    drugs = [f"clean{i}" for i in range(8)]
    counts, _ = _run(drugs, tmp_path, cap=4, max_runs=6, on_result=slow_on_result)
    assert counts["completed"] == 8
    assert harness["max"] == 4                        # promotes didn't idle the pool


def test_run_level_resume_reuses_cached_runs(harness, tmp_path):
    drugs = ["clean0", "tail0", "nocon0"]
    _run(drugs, tmp_path, cap=4, max_runs=6)
    first_calls = harness["calls"]
    assert first_calls > 0

    # Re-run over the same work dirs: every run_NN.json is cached -> zero fresh executions,
    # identical results.
    counts2, res2 = _run(drugs, tmp_path, cap=4, max_runs=6)
    assert harness["calls"] == first_calls            # no new pipeline runs
    assert counts2["completed"] == 3
    assert res2["clean0"]["status"] == "consensus" and res2["clean0"]["n_runs"] == 3
    assert res2["tail0"]["n_runs"] == 4
    assert res2["nocon0"]["status"] == "consensus_fallback"
    assert res2["nocon0"]["consensus"]["verdict"] == V


# --- identity capture/restore (the promote fix: reuse the run's own resolved identity,
#     never re-resolve at promote) ---------------------------------------------------------

def test_identity_capture_and_restore_roundtrip(tmp_path):
    """A run resolves identity into its scratch DB; the winner is promoted by writing THOSE
    rows as captured (get_identity_rows -> save_identity_rows), never a fresh LLM re-resolution.
    Verifies: capture drops the id + carries synonym_index; restore is idempotent (upsert +
    replace synonyms, no dup); fields land verbatim (proving no re-derivation)."""
    from core.scratch_db import scratch_db, dispose_scratch_db
    from core.db import (init_db, get_identity_rows,
                         save_identity_rows, DrugSynonym, SynonymIndex)
    src = f"sqlite:///{tmp_path}/src.db"
    with scratch_db(src):
        s = init_db()()
        s.add(DrugSynonym(drug_name="FooX", status="resolved", resolved_via="report:llm-pick",
                          cid="123", inchikey="AAAAAAAAAAAAAA-BBBBBBBBBB-N", chembl_id="CHEMBL1",
                          n_synonyms=2, sample_synonyms="FooX; Foo-X"))
        s.add(SynonymIndex(alias_norm="foox", alias_raw="FooX", canonical_drug_name="FooX",
                           source="pubchem:report-resolved", confidence="high"))
        s.add(SynonymIndex(alias_norm="fooxx", alias_raw="Foo-X", canonical_drug_name="FooX",
                           source="pubchem:report-resolved", confidence="high"))
        s.commit(); s.close()
        captured = get_identity_rows("FooX")
    dispose_scratch_db(src)

    assert captured["drug_synonyms"]["cid"] == "123"
    assert "id" not in captured["drug_synonyms"]           # id dropped so it re-inserts cleanly
    assert len(captured["synonym_index"]) == 2

    dst = f"sqlite:///{tmp_path}/dst.db"
    with scratch_db(dst):
        save_identity_rows("FooX", captured)
        save_identity_rows("FooX", captured)               # idempotent: replace, don't duplicate
        s = init_db()()
        assert s.query(DrugSynonym).filter_by(drug_name="FooX").count() == 1
        assert s.query(SynonymIndex).filter_by(canonical_drug_name="FooX").count() == 2
        row = s.query(DrugSynonym).filter_by(drug_name="FooX").first()
        assert (row.cid, row.status, row.resolved_via, row.chembl_id) == \
               ("123", "resolved", "report:llm-pick", "CHEMBL1")   # verbatim, not re-derived
        s.close()
    dispose_scratch_db(dst)


def test_identity_absent_capture_is_empty_and_save_is_noop(tmp_path):
    """A run that resolved nothing -> empty capture; save_identity_rows is a no-op that never
    raises (so promote of an unresolved drug leaves the target untouched)."""
    from core.scratch_db import scratch_db, dispose_scratch_db
    from core.db import (init_db, get_identity_rows,
                         save_identity_rows, DrugSynonym)
    url = f"sqlite:///{tmp_path}/empty.db"
    with scratch_db(url):
        cap = get_identity_rows("NopeX")
        assert cap == {"drug_synonyms": None, "synonym_index": []}
        save_identity_rows("NopeX", cap)                   # no-op, must not raise
        save_identity_rows("NopeX", None)                  # tolerate None too
        s = init_db()()
        assert s.query(DrugSynonym).filter_by(drug_name="NopeX").count() == 0
        s.close()
    dispose_scratch_db(url)
