# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Gemini CLI, Cursor and others) working in this repository. Read this first. It points to the document for each task; the facts live in those documents and in the code.

## What this repository is

Two things:

1. **The VALET pipeline.** It writes a report for each compound: does the published evidence support the compound's target? `simple_batch.py` runs it on a list of compounds, with Gemini and a local SQLite database.
2. **Paper Query.** `scripts/batch_paper_pipeline.py` checks the compounds that research papers (PDFs) use against the pipeline's reports.

| Path | Contents |
|---|---|
| `simple_batch.py` | Runs the pipeline on a list of compounds |
| `agents/` | The models' roles: specialists, PI, reviewers |
| `core/` | The pipeline: order of the stages, literature search, citation check, identity resolution, consensus, database |
| `prompts/` | The prompts of the pipeline and Paper Query |
| `scripts/build_pubchem_index.py` | Builds the offline PubChem index that identity resolution reads |
| `scripts/batch_paper_pipeline.py` | Paper Query, with `core/paper_query_engine.py`, `core/paper_drug_index.py` (the match) and `core/async_utils.py` (Gemini batch jobs) |
| `docs/` | How the pipeline and Paper Query work |
| `tests/` | `tests/pipeline/` pipeline tests; `tests/paper_query/` Paper Query tests |
| `data/` | Made by the pipeline when it runs: the database, logs, consensus runs, the PubChem index. Ignored by git |

## Setup

```bash
conda env create -f environment.yml
conda activate compound-valet
pytest
```

Use this environment's `python` and `pytest`, not the system ones. The tests need no network and no keys.

## What to read for each task

| Task | Read |
|---|---|
| Set up and run the pipeline | README: Pipeline, Settings, PubChem index; [docs/pipeline.md](docs/pipeline.md) |
| Understand or change how a report is made | [docs/pipeline.md](docs/pipeline.md), [docs/prompts.md](docs/prompts.md), then `agents/` and `prompts/` |
| Understand or change how runs are combined | [docs/consensus.md](docs/consensus.md), `core/consensus.py` |
| Read the results | [docs/database.md](docs/database.md), `core/db.py` |
| Look up a term (verdict, selectivity grade, PI…) | [docs/glossary.md](docs/glossary.md) |
| Run or change Paper Query | README: Paper Query; [docs/paper_query.md](docs/paper_query.md) |

## Rules

1. **Never read, print or log `.env`, API keys or other secrets.** To check that a setting exists, use `test -n "$NAME"`.
2. **Pipeline runs cost money.** Each run makes about a dozen Gemini calls (about $2.60; about $9 per compound with `--consensus`). So does Paper Query (a Gemini batch request per paper and a target check per matched compound use). Do not start either without the user's go.
3. **Keep the tests offline.** New tests must mock Gemini, Crossref and Semantic Scholar. Run `pytest` before you say a change is done.
4. **Do not change the pinned versions in `environment.yml` or `requirements.txt` without a reason.** Keep them in sync; after any change, build a fresh environment and run `pytest`.
5. **Write outputs outside the repository's tracked files.** The pipeline writes to `data/compound_valet.db`, `data/logs/` and `data/consensus_work/`, all ignored by git. Paper Query writes into the PDF folder it reads (`_pipeline_results/`, `pipeline_state.json`).
6. **A prompt change changes every later report.** Say so when you propose one, and say which prompt.
7. **Do not add `max_output_tokens` to Gemini calls.** The model's thinking counts against the limit, and a limit cuts the answer off. The pipeline's calls set none. Paper Query's batch request sets 65,536, the value its runs used.
8. **`target` and `target_gene_names` are different fields.** `target` is text; `target_gene_names` is a list of HGNC gene symbols.

## Documentation

- The code is the source of truth. If a document disagrees with the code, fix the document, or ask the user if the code looks wrong.
- Each fact lives in one document; others link to it.
- Keep this file short: pointers, not facts.
