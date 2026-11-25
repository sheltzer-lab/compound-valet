# Consensus

Two runs of the pipeline on one compound can give different answers (see [pipeline.md](pipeline.md)). With `--consensus`, `simple_batch.py` runs each compound several times and saves the run that the runs agree on.

The rule is in `core/consensus.py`. The runs are scheduled by `core/consensus_runner.py::run_batch_consensus`.

## When two runs agree

`pair_agrees` compares two runs:

1. Different verdicts: they do not agree.
2. Both UNVERIFIED: they agree. Nothing was established, so target and selectivity do not matter.
3. Both VERIFIED: the selectivity grades must be the same, and the targets must match:
   - both name no specific target ("None", "Unknown", no gene symbols): they agree;
   - one names a target and the other does not: they do not agree;
   - both name a target: Gemini decides whether they are the same target (`core/target_matching.py`, `GEMINI_TARGET_MATCH_MODEL`, default `gemini-3.8-flash`, `prompts/target_match_prompt.md`). If that call fails after retries, the pair counts as not agreeing.

A missing selectivity grade counts as UNKNOWN. INCONCLUSIVE and PARTIAL verdicts count as UNKNOWN.

## When a compound is done

`decide_consensus` looks at the runs so far, in run order, with `n` = `--consensus-n` (default 3):

1. **Verdict.** The first verdict to reach `n` runs wins. If VERIFIED wins but `n + 1` of the runs come back UNVERIFIED, UNVERIFIED takes the verdict back, so a compound is not recorded as VERIFIED on a minority of its runs.
2. **UNVERIFIED wins:** done.
3. **VERIFIED wins:** runs continue until `n` VERIFIED runs agree with each other on every pair (rule above). Then done.

The runner starts `n` runs, then adds one run at a time until the compound is done or `--consensus-max-runs` runs (default 8) have been made. A run that fails (for example when Gemini cannot be reached) is not compared, but it counts toward that limit. If every run fails, nothing is saved and the compound is not marked finished, so a restarted batch runs it again.

## Which run is saved

| Outcome | Status in `consensus_stats.csv` | Saved run |
|---|---|---|
| Runs agreed | `consensus` | Of the agreeing runs, the one with the most verified papers (ties: the earliest run). For UNVERIFIED, the agreeing runs are the first `n` UNVERIFIED runs |
| VERIFIED won, but no `n` runs agreed on target and selectivity by the last run | `consensus_fallback` | The most common selectivity grade among the VERIFIED runs; of those runs, the one with the most verified papers |
| No verdict reached `n` runs | `no_consensus` | The run with the most verified papers |

Each run works in its own temporary database (`core/scratch_db.py`), so runs of one compound do not see each other's papers. Only the saved run is written to the real database: its report and papers in one transaction, then its identity (`core/consensus_runner.py::promote_winner`).

With `--consensus`, compounds already in the database are run again and replaced. A restart must use the same `GEMINI_MODEL_NAME` as the batch it continues (`model.txt` in the work folder); otherwise it stops with a message. A restarted batch skips only the compounds in its `completed_drugs.csv`, and reuses every run it already has (`run_NN.json`), including the runs after one that failed.
