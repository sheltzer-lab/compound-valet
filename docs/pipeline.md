# The VALET pipeline

The pipeline writes one report for one compound. It decides whether the compound's stated target is supported by the published evidence (VERIFIED or UNVERIFIED), names the target and its genes, and grades the compound's selectivity.

`core/pipeline.py::run_analysis_pipeline` is the source of truth for the order of the stages. If this page and the code disagree, the code is right; fix the page.

## Stages

```
compound name
  │
  ▼  A. Literature search and citation check          core/ingestion.py: build_initial_context
  │     3 grounded Gemini searches ─► candidate papers ─► check each paper ─► verified papers
  ▼  B. Check: the run's papers equal the database's  core/pipeline.py
  ▼  C. Specialists, in parallel                      agents/specialists/{genetics,biochemistry,biophysics}.py
  ▼  D. PI draft                                      agents/pi.py: generate_pi_report
  ▼  E. Identity resolution (PubChem structure)       core/identity_resolver.py: resolve_identity_from_report
  ▼  F. Reviewers, in parallel                        agents/reviewers.py: generate_reviewer_report
  ▼  G. Papers the reviewers say are missing          core/pipeline.py: _add_reviewer_suggested_papers
  ▼  H. PI revision                                   agents/pi.py: revise_pi_report
  ▼  I. Citation links                                core/citation_utils.py: resolve_paper_citations
  ▼  J. Save report and papers                        core/db.py: promote_job_papers
report
```

### A. Literature search and citation check

`build_initial_context` runs three Gemini searches with Google Search grounding, in parallel, one for each search angle in `PromptFactory.SEARCH_ANGLES` (`core/prompt_factory.py`):

| Angle | Looks for |
|---|---|
| Mechanism | The discovery paper, crystal structures, binding affinities (Kd, IC50) |
| Genetics | CRISPR screens, resistance mutations, knockout studies |
| Clinical | Clinical trials, PK/PD, safety and toxicity |

The prompt is `prompts/literature_search_simple.md`. Each search returns a list of papers. Duplicates are removed (`core/utils.py::is_duplicate`). If the Gemini call of every search fails (an outage, an expired login), the run fails and nothing is saved.

Then `core/citation_manager.py::run_citation_manager` checks every candidate paper against Crossref and Semantic Scholar (`core/references/`). A paper stays only if Crossref or Semantic Scholar has a record with a matching title and its link is safe. If only a shortened title matches, the record must also have the paper's year and at least one of its authors' last names, where the search gave them. The check also asks Crossref whether the paper was retracted (`core/retraction_live.py`). If checking one paper fails, that paper is left out and the others are still checked.

The papers that pass are numbered `<compound>-Paper-1`, `-Paper-2`, and so on, and saved apart from the papers of saved reports until the run is saved (stage J): a single run keeps them under its own ID in `job_scoped_verified_papers`; a consensus run keeps them in its own temporary database ([database.md](database.md)).

### B. Only verified papers go further

The pipeline asserts that the papers it carries equal the papers in the database. Every later stage reads only these papers. The models can cite only papers that passed the citation check.

### C. Specialists

Three specialists read the verified papers, in parallel. They do not see each other's reports.

| Specialist | Prompt | Looks for |
|---|---|---|
| Genetics | `prompts/genetics_prompt.md` | Genetic evidence: knockouts, knockdowns, resistance mutations, screens |
| Biochemistry | `prompts/biochemistry_prompt.md` | Binding and activity: Kd, IC50, enzyme assays, selectivity panels |
| Biophysics | `prompts/biophysics_prompt.md` | Direct binding: SPR, ITC, co-crystal and cryo-EM structures |

Each returns a markdown report. If a specialist's call fails or returns nothing, its report is an error message and the run goes on.

### D. PI draft

The PI ("principal investigator") weighs the three specialist reports and writes the draft report: the verdict, the target and target genes, the selectivity grade, the evidence matrix, and a JSON block with the same fields (`prompts/pi_draft.md`, which includes `pi_persona.md` and `pi_report_format.md`). `agents/pi.py::process_pi_response` parses the JSON block; if it is missing, it reads the fields from the markdown. If the draft call fails or returns nothing, the draft is an error message; the revision (stage H) still writes the final report. If the revision fails too, the run fails and nothing is saved.

### E. Identity resolution

`core/identity_resolver.py` finds the PubChem structure the compound name refers to, using the draft's conclusion to choose between candidates (`prompts/identity_candidate_pick.md`, `identity_synonym_filter.md`). It writes the compound's identity (InChIKey, registry IDs, SMILES) to `drug_synonyms` and its aliases to `synonym_index`. Reviewers #1 and #2 get a summary, so they can check that the report is about the right compound.

It needs the offline PubChem index (see the README). Without the index this stage is skipped and the log says so.

### F. Reviewers

Three reviewers read the draft, in parallel:

| Reviewer | Prompt | Temperature | top_p | Role |
|---|---|---|---|---|
| Reviewer #1 | `prompts/reviewer_neutral.md` | 0.9 | 0.99 | Exploratory: other readings of the evidence, missing papers |
| Reviewer #2 | `prompts/reviewer_critic.md` | 0.05 | 0.1 | Critic: errors in the draft |
| Selectivity Reviewer | `prompts/reviewer_selectivity.md` | 0.05 | 0.1 | The selectivity grade only (advisory); reads the specialist reports |

`core/pipeline.py` starts the reviewers and sets these values. Each reviewer gets 3 attempts. If all fail, its review is an error message and the pipeline goes on.

### G. Papers the reviewers say are missing

A reviewer can list papers the draft should have used. These go through the same citation check as in stage A. The ones that pass are added to the run's papers, and the PI revision gets a list of them (`prompts/missing_papers_section.md`). The ones that fail are noted at the end of that reviewer's report.

### H. PI revision

The PI reads its draft, the three reviews and the added papers, and writes the final report (`prompts/pi_revision_prompt.md`). 3 attempts; if all fail, the draft stands.

### I. Citation links

The reports cite papers as `[Imatinib-Paper-3]`. `resolve_paper_citations` replaces each with ``[Author, Year](link)`` from the verified papers.

### J. Save

`core/analysis_schema.py::build_analysis_result` assembles the result. `promote_job_papers` writes the report and its papers in one transaction. A single-run batch saves a compound that is not in the database yet, or, with `--force`, one that is. In a consensus batch, each run works in its own temporary database and only the run the others agree with is saved, with its identity ([consensus.md](consensus.md)). See [database.md](database.md) for the tables.

## Models and settings

| Call | Model | Settings |
|---|---|---|
| Search, specialists, PI, reviewers, revision | `GEMINI_MODEL_NAME`, default `gemini-3.1-pro-preview` (`core/llm.py`) | Google Search grounding; temperature 0.05, top_p 0.1 unless a reviewer overrides them (`core/model_config.py`); no output-token limit |
| Identity resolution | `GEMINI_MODEL_NAME`, default `gemini-3.1-pro-preview` | No search; temperature 0 |
| Consensus target check | `GEMINI_TARGET_MATCH_MODEL`, default `gemini-3.8-flash` (`TARGET_MATCH_MODEL`, `core/target_matching.py`) | JSON answer; temperature 0 |

`core/client_factory.py` uses Vertex AI when `GOOGLE_CLOUD_PROJECT` is set, else the Gemini API with `GOOGLE_API_KEY`. On Vertex AI, the Gemini client makes up to 5 attempts at a request that fails with HTTP 408, 429, 500, 502, 503 or 504. `core/llm.py` also retries:

- after a rate limit (429), it waits (20 seconds, doubling each time) and tries again, up to 5 times;
- after a 502, 504 or 499, it waits and tries again, up to 2 times;
- after a 503 (service unavailable), it pauses all Gemini calls for 3 minutes (at most 10 minutes in a row) and tries again, up to 2 times.

Gemini does not give the same text twice, even at temperature 0.05. Two runs of one compound can disagree, mostly for compounds near the line between VERIFIED and UNVERIFIED. That is why the batch command has a consensus mode.

## Running compounds: `simple_batch.py`

```bash
python simple_batch.py compounds.txt --consensus
```

| Option | Default | What it does |
|---|---|---|
| `--consensus` | off | Run each compound until runs agree, and save the run they agree on. See [consensus.md](consensus.md) |
| `--consensus-n` | 3 | Runs that must agree |
| `--consensus-max-runs` | 8 | Give up on agreement after this many runs |
| `--workers` | 10 | Runs at the same time |
| `--force` | off | Without `--consensus`: run compounds that are already in the database again; the new report and its papers replace the old ones |
| `--progress-interval` | 10 | Log progress every N finished compounds (for a short list, every fifth of the list) |
| `--consensus-work-dir` | `data/consensus_work/<list name>` | Where each run's result is kept |
| `--discard-run-reports` | off | Delete each compound's run results when it finishes, instead of keeping them under `_reports/` |

Without `--consensus`, each compound runs once and is saved. A name listed more than once is run once. A run that fails is not saved: the compound is listed in `failed_drugs.csv`, and a compound already in the database keeps its report, even with `--force`.

Before any run, the batch checks the settings and Gemini access (`core/client_factory.py::check_gemini_setup`). For each model the batch will use, it asks Gemini to count the tokens of a short text, which is free and needs the same access as a real call. It stops, with one message, if no Gemini setting is present, there is no Google Cloud login or it has expired, the API key is not valid, Gemini refuses access (HTTP 401 or 403), or a model is not available in the location (HTTP 404). Any other error (network, 429, 5xx) is only logged, and the batch goes on.

Files it writes:

| File | Contents |
|---|---|
| `data/compound_valet.db` | The database ([database.md](database.md)), unless `DATABASE_URL` names another SQLite file |
| `data/logs/streaming_batch.log` | The log |
| `data/logs/failed_drugs.csv` | Compounds whose run failed, with the error |
| `data/consensus_work/<list>/<compound>/run_NN.json` | Each run's result. A restarted batch reuses them. The folder is the compound name with punctuation replaced; when two names in the list would share a folder (for example (+)- and (−)-forms), each gets a short code added |
| `data/consensus_work/<list>/completed_drugs.csv` | Compounds finished. A restarted batch skips them |
| `data/consensus_work/<list>/model.txt` | The report model (`GEMINI_MODEL_NAME`) the batch started with. A restart with another model stops with a message, so one compound's runs never mix two models |
| `data/consensus_work/<list>/consensus_stats.csv` | One row per compound: outcome, number of runs, the winning run |
| `data/consensus_work/<list>/run_timings.csv` | Time per run |
| `data/consensus_work/<list>/_reports/<compound>/` | All runs of a finished compound |

To stop a batch, press Ctrl-C once: runs in progress finish, then it stops. Press it again to stop at once.

## Cost

Gemini charges per token and per Google Search query. `core/billing.py` estimates each run's cost from the run's token and search counts, and it is saved in `analysis_results.cost` and `usage_stats`. In our runs, one run cost about $2.60. With `--consensus`, `analysis_results.cost` holds the saved run's cost only; add up the `cost` in the compound's `run_NN.json` files for the total.
