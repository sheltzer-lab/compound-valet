
import os
import sys
import logging

# Logs live under data/logs/, relative to the folder it is run from.
_log_dir = "data/logs"
log_file = os.path.join(_log_dir, "streaming_batch.log")

# Silence ALL noisy libraries BEFORE importing them
for lib in ["urllib3", "google", "httpx", "httpcore", "hpack", "h2", "grpc", "asyncio", "google_genai"]:
    logging.getLogger(lib).setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

# Now safe to import
import argparse
import signal
import concurrent.futures
import traceback
from datetime import datetime
import time

# Ensure repository root is on sys.path
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from core.pipeline import run_analysis_pipeline
from core.db import get_analysis, init_db
from core.client_factory import check_gemini_setup, GeminiSetupError
from core.llm import MODEL_NAME as REPORT_MODEL
from core.target_matching import TARGET_MATCH_MODEL
import csv
import threading

# Thread-safe counters and state
csv_lock = threading.Lock()
csv_file = os.path.join(_log_dir, "failed_drugs.csv")

# Graceful shutdown
shutdown_requested = False

def signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    global shutdown_requested
    if not shutdown_requested:
        logger.warning("⚠️ Shutdown requested - finishing in-flight work...")
        shutdown_requested = True
    else:
        logger.warning("⚠️ Force exit requested")
        # Not sys.exit: it would wait for the runs in progress, because the worker
        # threads are joined at exit. Nothing is left half-saved: a run's report and
        # papers are saved in one transaction, and a restart redoes unfinished work.
        os._exit(1)


def _setup_runtime():
    """Configure file logging, signal handlers, and the failed-drugs CSV.

    Called from main() after argument parsing so importing this module or running
    `simple_batch.py --help` creates no files and prints no stray log lines.
    """
    os.makedirs(_log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file)
        ],
        force=True,
    )
    for lib in ["urllib3", "google", "httpx", "httpcore", "hpack", "h2", "grpc", "asyncio", "google_genai"]:
        logging.getLogger(lib).setLevel(logging.ERROR)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if not os.path.exists(csv_file):
        with open(csv_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['drug_name', 'error', 'timestamp'])

def log_failure(drug_name, error_msg):
    with csv_lock:
        os.makedirs(_log_dir, exist_ok=True)
        with open(csv_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([drug_name, error_msg, datetime.now().isoformat()])

# --- Consensus mode (first-to-N) ---------------------------------------------------
from pathlib import Path as _Path
import shutil as _shutil
from core.consensus_runner import run_batch_consensus, promote_winner

# Populated in main() when --consensus is on.
_ARGS = None
_RUN_POOL = None            # shared, size-bounded pipeline-run pool (the resource cap)
_WORK_ROOT = None           # stable per-file dir: resume artifacts + checkpoint live here
_COMPLETED_CSV = None
_STATS_CSV = None
_COMPLETED = set()          # drugs already promoted this batch (drug-level resume)
_consensus_lock = threading.Lock()


def _slug(name):
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "drug"


_WORK_NAMES = {}            # drug name -> its folder under _WORK_ROOT (set in main)


def _work_folder_names(names):
    """Folder for each drug's consensus runs: the slug of its name. When two names in the
    batch share a slug ('(+)-X' and '(-)-X', or names that differ only in case on a
    case-insensitive disk), each gets a short hash of its exact name added, so their
    runs never share a folder."""
    import hashlib
    by_slug = {}
    for n in names:
        by_slug.setdefault(_slug(n).lower(), []).append(n)
    out = {}
    for group in by_slug.values():
        for n in group:
            out[n] = _slug(n) if len(group) == 1 else f"{_slug(n)}_{hashlib.sha1(n.encode('utf-8')).hexdigest()[:8]}"
    return out


def _work_folder(drug_name):
    return _WORK_NAMES.get(drug_name) or _slug(drug_name)


WORK_DIR_MODEL_FILE = "model.txt"


def check_work_dir_model(work_dir, model):
    """Pin a consensus work folder to one report model.

    Writes model.txt on first use, accepts the same model afterwards, and refuses a different
    one: the folder holds finished runs from the earlier model, and consensus compares runs
    against each other.
    """
    path = os.path.join(work_dir, WORK_DIR_MODEL_FILE)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(model + "\n")
        return
    with open(path) as f:
        recorded = f.read().strip()
    if recorded != model:
        raise ValueError(
            f"Work folder {work_dir} was started with the model {recorded}, but this run uses {model}. "
            f"Set GEMINI_MODEL_NAME={recorded} to continue it, or start fresh with a new --consensus-work-dir.")


def _load_completed(path):
    done = set()
    if path and os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.reader(f):
                if row:
                    done.add(row[0])
    return done


def _append_completed(drug_name):
    with _consensus_lock:
        with open(_COMPLETED_CSV, "a", newline="") as f:
            csv.writer(f).writerow([drug_name, datetime.now().isoformat()])
        _COMPLETED.add(drug_name)


def _write_stats(drug_name, res):
    spread = [r["n_final_papers"] for r in res["runs"]]
    con = res["consensus"] or {}
    row = [drug_name, res["status"], res["n_runs"], res.get("winner_run_index"),
           min(spread) if spread else "", max(spread) if spread else "",
           con.get("verdict", ""), con.get("target", ""), con.get("selectivity", "")]
    with _consensus_lock:
        new = not os.path.exists(_STATS_CSV)
        with open(_STATS_CSV, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["drug", "status", "n_runs", "winner_run", "min_papers",
                            "max_papers", "verdict", "target", "selectivity"])
            w.writerow(row)


def _promote_consensus_result(drug_name, res):
    """on_result callback for run_batch_consensus: promote the winner to the target DB, then
    checkpoint + write stats + archive the per-run reports. Runs on the scheduler's SERIAL
    promote thread, so a DB write never stalls scheduling. Called once per drug."""
    if res["winner"] is None:
        logger.error(f"❌ {drug_name} - all consensus runs failed")
        log_failure(drug_name, "all consensus runs failed")
        return

    # Promote the winner to the TARGET DB (the only write this drug makes), THEN
    # checkpoint — a crash mid-promote leaves the drug un-checkpointed and re-done.
    promote_winner(drug_name, res["winner"], res["winner_papers"], res.get("winner_identity"))
    _append_completed(drug_name)
    _write_stats(drug_name, res)
    # By default, retain the per-run reports (run_NN.json — every member's full report +
    # papers) by archiving them under <work_root>/_reports/, so canonical-selection quality
    # can be assessed after the fact (winner vs losers). --discard-run-reports drops them.
    work_dir = os.path.join(_WORK_ROOT, _work_folder(drug_name))
    try:
        if getattr(_ARGS, "discard_run_reports", False):
            _shutil.rmtree(work_dir)
        else:
            dest = os.path.join(_WORK_ROOT, "_reports", _work_folder(drug_name))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                _shutil.rmtree(dest)
            _shutil.move(work_dir, dest)
    except OSError:
        pass
    logger.info(f"✅ CONSENSUS DONE: {drug_name} [{res['status']}] {res['n_runs']} runs, "
                f"winner run {res.get('winner_run_index')}")


def process_drug(drug_name, force=False):
    """
    Worker function to process a single drug end-to-end.
    Returns (success: bool, skipped: bool, duration_seconds: float)
    """
    start_time = time.time()

    # Check for shutdown
    if shutdown_requested:
        logger.debug(f"Skipping {drug_name} - shutdown requested")
        return False, True, 0

    # (Consensus mode is handled by run_batch_consensus in main(), not per-drug here.)

    # RESUMABILITY CHECK
    if not force:
        existing = get_analysis(drug_name)
        if existing:
            logger.info(f"⏭️ {drug_name} - already in DB, skipping")
            return True, True, 0

    logger.info(f"🚀 STARTING: {drug_name}")
    try:
        # Run the full pipeline
        # The pipeline saves the report and its papers together; with --force it saves
        # over a drug that is already in the database.
        run_analysis_pipeline(
            drug_name=drug_name,
            model_name=REPORT_MODEL,
            auto_save=True,
            overwrite=force,
        )

        duration = time.time() - start_time
        logger.info(f"✅ COMPLETED: {drug_name} ({duration:.1f}s)")
        return True, False, duration
    except Exception as e:
        error_msg = str(e)
        tb = traceback.format_exc()
        duration = time.time() - start_time
        logger.error(f"❌ {drug_name} - FAILED after {duration:.1f}s: {error_msg[:100]}\n{tb}")
        log_failure(drug_name, error_msg)
        return False, False, duration

def format_eta(seconds):
    """Format seconds as human-readable time."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"

def main():
    parser = argparse.ArgumentParser(description="Run the Compound VALET pipeline on a list of compounds")
    parser.add_argument("file", help="File containing list of drugs (one per line)")
    parser.add_argument("--workers", type=int, default=10, help="Number of concurrent workers (default: 10)")
    parser.add_argument("--force", action="store_true",
                        help="without --consensus: run drugs already in the DB again, replacing their report and papers")
    parser.add_argument("--progress-interval", type=int, default=10, help="Log progress every N completions")
    parser.add_argument("--consensus", action="store_true",
                        help="run first-to-N consensus per drug; save the canonical winner")
    parser.add_argument("--consensus-n", type=int, default=3,
                        help="runs that must mutually agree (default 3)")
    parser.add_argument("--consensus-max-runs", type=int, default=8,
                        help="give up on a drug after this many runs, no consensus (default 8)")
    parser.add_argument("--consensus-work-dir", default=None,
                        help="dir for per-run resume artifacts + checkpoint "
                             "(default data/consensus_work/<drugfile>)")
    parser.add_argument("--discard-run-reports", action="store_true",
                        help="delete each drug's per-run reports on completion instead of "
                             "archiving them under <work_root>/_reports (saves disk)")

    args = parser.parse_args()
    _setup_runtime()

    # Warn if running with many workers
    if args.workers > 20:
        logger.warning(f"⚠️ Running with {args.workers} workers - watch for 429 rate limits")

    # 2. Load Drugs
    drugs = []
    if os.path.exists(args.file):
         with open(args.file, "r") as f:
            drugs = [l.strip() for l in f.readlines() if l.strip()]
    else:
        logger.error(f"File not found: {args.file}")
        sys.exit(1)

    # A name listed twice would be run twice at the same time; run it once.
    repeated = sorted({d for d in drugs if drugs.count(d) > 1})
    if repeated:
        logger.warning(f"⚠️ Listed more than once, run once: {', '.join(repeated)}")
        drugs = list(dict.fromkeys(drugs))
    total = len(drugs)

    # Stop now, with a clear message, if Gemini cannot be used with these settings.
    try:
        check_gemini_setup([REPORT_MODEL] + ([TARGET_MATCH_MODEL] if args.consensus else []))
    except GeminiSetupError as e:
        logger.error(f"❌ {e}")
        sys.exit(1)

    # Set up the database here, before any worker starts: two workers opening a brand-new
    # database at the same moment can query a table before it has been created.
    try:
        init_db()
    except Exception as e:
        logger.error(f"❌ {e}")
        sys.exit(1)

    logger.info(f"📋 Loaded {total} drugs | Workers: {args.workers} | Force: {args.force}")

    # 3. Stream Processing with Progress Tracking
    start_time = time.time()
    completed = 0
    failed = 0
    skipped = 0
    total_processing_time = 0
    processed_count = 0  # Non-skipped drugs
    
    # Auto-adjust progress interval for small batches
    progress_interval = min(args.progress_interval, max(1, total // 5))
    logger.info(f"📊 Progress updates every {progress_interval} completions")

    # Consensus setup: ONE shared, size-bounded pipeline-run pool (the resource cap =
    # --workers total runs), driven by fewer drug drivers so we don't open workers x N
    # runs at once. Resume state (per-run artifacts + the drug checkpoint) lives in a
    # stable per-file work dir so a restart continues instead of redoing finished work.
    global _ARGS, _RUN_POOL, _WORK_ROOT, _COMPLETED_CSV, _STATS_CSV, _COMPLETED
    _ARGS = args
    pool_workers = args.workers
    if args.consensus:
        _WORK_ROOT = args.consensus_work_dir or os.path.join("data", "consensus_work", _Path(args.file).stem)
        os.makedirs(_WORK_ROOT, exist_ok=True)
        # A restart must not mix runs from two models inside one compound's consensus.
        try:
            check_work_dir_model(_WORK_ROOT, REPORT_MODEL)
        except ValueError as e:
            logger.error(f"❌ {e}")
            sys.exit(1)
        _COMPLETED_CSV = os.path.join(_WORK_ROOT, "completed_drugs.csv")
        _STATS_CSV = os.path.join(_WORK_ROOT, "consensus_stats.csv")
        _COMPLETED = _load_completed(_COMPLETED_CSV)
        _WORK_NAMES.update(_work_folder_names(drugs))
        _RUN_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
        # Drivers = run budget. Fewer starves the run pool the moment drugs enter their
        # tail (adding a 4th/5th run one at a time, demanding <consensus_n slots); with
        # drivers = workers there are always enough open drugs to keep all args.workers
        # run-slots full.
        pool_workers = args.workers
        logger.info(f"🧫 Consensus: n={args.consensus_n} max_runs={args.consensus_max_runs} | "
                    f"run budget {args.workers}, {pool_workers} drug drivers | "
                    f"work={_WORK_ROOT} | {len(_COMPLETED)} already done")

    if args.consensus:
        # One pool-filling scheduler across all drugs — keeps _RUN_POOL saturated even when
        # drugs enter their one-at-a-time tail (no per-drug driver to strand slots). Filter
        # out drugs already done in this list's completed_drugs.csv first (drug-level resume).
        todo = []
        for _d in drugs:
            # Resume-skip on the completed_drugs.csv checkpoint ONLY — NOT get_analysis. A drug
            # that partial-promoted (analysis_results written) then died before the checkpoint
            # must be RE-RUN, not skipped: keying on get_analysis would see that stray row and
            # silently orphan the drug. The checkpoint is written
            # only after a full, successful promote, so it is the authoritative "done" signal.
            if _d in _COMPLETED:
                skipped += 1
            else:
                todo.append(_d)
        logger.info(f"🧫 consensus: {len(todo)} to run, {skipped} already done (completed_drugs.csv)")
        prog = {"done": 0, "ok": 0, "fail": 0}

        def _on_result(drug, res):
            _promote_consensus_result(drug, res)
            prog["done"] += 1
            prog["fail" if res["winner"] is None else "ok"] += 1
            if prog["done"] % progress_interval == 0 or prog["done"] == len(todo):
                el = time.time() - start_time
                rate = prog["done"] / el * 3600 if el > 0 else 0
                logger.info(f"📊 consensus [{prog['done']}/{len(todo)}] | "
                            f"✅ {prog['ok']} ❌ {prog['fail']} | Rate: {rate:.0f}/hr")

        c = run_batch_consensus(
            todo, model_name=REPORT_MODEL, consensus_n=args.consensus_n,
            max_runs=args.consensus_max_runs, run_pool=_RUN_POOL,
            work_dir_for=lambda d: os.path.join(_WORK_ROOT, _work_folder(d)),
            on_result=_on_result, should_stop=lambda: shutdown_requested,
            log=lambda m: logger.info(m))
        completed, failed = c["completed"], c["failed"]
        processed_count = completed + failed
        drugs = []   # the executor loop below is a no-op in consensus mode

    with concurrent.futures.ThreadPoolExecutor(max_workers=pool_workers) as executor:
        futures = {executor.submit(process_drug, drug, args.force): drug for drug in drugs}
        
        for future in concurrent.futures.as_completed(futures):
            drug = futures[future]
            try:
                success, was_skipped, duration = future.result()
                if was_skipped:
                    skipped += 1
                elif success:
                    completed += 1
                    total_processing_time += duration
                    processed_count += 1
                else:
                    failed += 1
                    processed_count += 1
            except Exception as e:
                logger.error(f"❌ {drug} - exception: {e}")
                failed += 1
                processed_count += 1
            
            # Progress update every N completions
            done = completed + failed + skipped
            if done % progress_interval == 0 or done == total:
                elapsed = time.time() - start_time
                rate = processed_count / elapsed * 3600 if elapsed > 0 and processed_count > 0 else 0
                remaining = total - done
                eta_seconds = (remaining / (processed_count / elapsed)) if processed_count > 0 and elapsed > 0 else 0
                
                logger.info(
                    f"📊 [{done}/{total}] {done*100/total:.1f}% | "
                    f"✅ {completed} ❌ {failed} ⏭️ {skipped} | "
                    f"Rate: {rate:.0f}/hr | ETA: {format_eta(eta_seconds)}"
                )
            
            # Check for shutdown after each completion
            if shutdown_requested:
                logger.warning(f"⚠️ Shutdown: cancelling remaining work...")
                executor.shutdown(wait=False, cancel_futures=True)
                break
    
    if _RUN_POOL is not None:
        _RUN_POOL.shutdown(wait=True)

    # Final stats
    elapsed = time.time() - start_time
    logger.info(f"{'🛑 INTERRUPTED' if shutdown_requested else '✨ BATCH FINISHED'}")
    logger.info(f"   Completed: {completed} | Failed: {failed} | Skipped: {skipped}")
    logger.info(f"   Total time: {format_eta(elapsed)}")
    if processed_count > 0:
        if args.consensus:
            logger.info(f"   Avg wall time per drug: {elapsed/processed_count:.1f}s")
        else:
            logger.info(f"   Avg time per drug: {total_processing_time/processed_count:.1f}s")

if __name__ == "__main__":
    main()
