# Compound VALET

The VALET pipeline writes a report for each compound: does the published evidence support the compound's stated target? This repository holds the pipeline ([Pipeline](#pipeline)) and Paper Query, which checks the compounds research papers use against those reports ([Paper Query](#paper-query)).

AI coding agents: read [AGENTS.md](AGENTS.md) first.

## Setup

```bash
conda env create -f environment.yml
conda activate compound-valet
pytest
```

Or with Python 3.11 and `pip` in a virtual environment: `pip install -r requirements.txt && pytest`.

The tests need no keys and no network.

## Pipeline

`simple_batch.py` runs the VALET pipeline on a list of compounds. For each compound it searches the literature and checks the citations, writes three specialist reports and a draft PI report, resolves the compound's structure in PubChem, runs three reviewers, and revises the PI report.

**To view the existing reports, you do not need to run anything.** The reports from our batch runs, the best existing data from this system, are on the website: [compoundvalet.ai](https://compoundvalet.ai).

**Running the pipeline needs Gemini access and costs money.**

- Gemini access: through Vertex AI (on Google Cloud, with a Cloud Project) or Google AI Studio (with a Gemini API key). See [Settings](#settings).
- Cost: you pay for the Gemini tokens and Google Search queries. In our runs, one run cost about $2.60 ([docs/pipeline.md](docs/pipeline.md#cost)).

```bash
printf "Imatinib\nOTS964\n" > compounds.txt
python simple_batch.py compounds.txt --consensus
```

`compounds.txt` has one compound name per line.

- With `--consensus`, each compound runs until 3 runs agree (at most 8 runs), and the run they agree on is saved. A compound already in the database is run again and its report replaced; a restarted batch skips only the compounds it has finished.
- Without `--consensus`, each compound runs once. A compound already in the database is skipped unless you add `--force`, which replaces its report and papers.

`python simple_batch.py --help` lists the other options.

Before the first run, the batch checks the settings, and that each Gemini model it will use can be reached (a token count, which is free). It stops with a message that says what to fix if a setting is missing, there is no Google Cloud login or it has expired, the API key is not valid, Gemini refuses access, or a model is not available in the location.

Run it from the repository folder. It writes to a SQLite database, `data/compound_valet.db`, and its logs to `data/logs/`, under the folder it is run from. It calls Gemini: `GEMINI_MODEL_NAME` (default `gemini-3.1-pro-preview`) with Google Search grounding for the reports, and `GEMINI_TARGET_MATCH_MODEL` (default `gemini-3.8-flash`) to check whether two runs name the same target. Gemini can answer differently from run to run, so a new run can give a different report.

To read a saved report back out of the database:

```bash
python -c "from core.db import get_analysis; r = get_analysis('Imatinib'); print(r['structured_summary']['final_verdict'], r['report'][:500])"
```

### Settings

The pipeline reads its settings from environment variables. Copy `.env.example` to `.env` in the repository folder and fill in the values you need; the pipeline loads `.env` when it starts, and `.gitignore` keeps it out of git.

| Setting | What it is |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project, for Gemini through Vertex AI. Log in first with `gcloud auth application-default login` |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI location. Set it to `global`: the default, `us-central1`, may not serve these models |
| `GOOGLE_API_KEY` | Gemini API key, from Google AI Studio. Used only when `GOOGLE_CLOUD_PROJECT` is not set |
| `SEMANTIC_SCHOLAR_API_KEY` | Optional. Semantic Scholar API key. Without it, the pipeline sends 1 request per second to Semantic Scholar; with it, 5 |
| `CROSSREF_MAILTO` | Optional. Your email address. Crossref gives better service to requests that name a contact |
| `CROSSREF_API_TOKEN` | Optional. Crossref Metadata Plus token |
| `API_RATE_DIVISOR` | Optional. If you run N pipeline processes at once, set it to N, so that together they keep to the Crossref and Semantic Scholar rates |
| `PUBCHEM_INDEX` | Optional. Path to the PubChem index (below). Default: `data/pubchem/pubchem_offline.sqlite` |
| `PUBCHEM_FLAT_DIR` | Optional. Folder holding PubChem's bulk `.gz` files for `scripts/build_pubchem_index.py`. Default: `data/pubchem/flat` |
| `DATABASE_URL` | Optional. SQLite URL of the database to write to, for example `sqlite:///data/other.db`. Default: `data/compound_valet.db` |
| `GEMINI_MODEL_NAME` | Optional. The Gemini model used for reports, identity resolution, and Paper Query's extraction. Default: `gemini-3.1-pro-preview` |
| `GEMINI_TARGET_MATCH_MODEL` | Optional. The Gemini model that checks whether two targets match (consensus and Paper Query). Default: `gemini-3.8-flash` |
| `GCS_BUCKET_NAME` | Paper Query only. Cloud Storage bucket in your Google Cloud project, for its Gemini batch jobs on Vertex AI |

### PubChem index

Identity resolution finds the PubChem structure a compound name refers to. It reads an offline index that you build once from PubChem's bulk files:

1. Download `CID-Synonym-filtered.gz`, `CID-Parent.gz` and `CID-SMILES.gz` (about 3 GB together) from https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/ into `data/pubchem/flat/` (or another folder, named in the setting `PUBCHEM_FLAT_DIR` or the option `--src`).
2. From the repository folder, run `python scripts/build_pubchem_index.py --full`. It takes 30 to 90 minutes and writes `data/pubchem/pubchem_offline.sqlite` (about 29 GB).

Without the index, the pipeline still runs. It skips identity resolution.

## Paper Query

`scripts/batch_paper_pipeline.py` reads a folder of research papers (PDFs, named `First author et al. - YYYY - Title.pdf`). For each paper it finds the compounds the authors use for a stated target, matches each one to a compound in a VALET database, and checks the paper's target against the compound's report. Each use is marked verified, unverified, or not in the database ([docs/paper_query.md](docs/paper_query.md)).

```bash
python scripts/batch_paper_pipeline.py <folder of PDFs> --year_cutoff 2000
```

It needs Gemini on Vertex AI (`GOOGLE_CLOUD_PROJECT`), a Cloud Storage bucket (`GCS_BUCKET_NAME`), and a populated VALET database (run `simple_batch.py` first or point `DATABASE_URL` at one; Paper Query stops before submitting a batch job if the database is empty unless you pass `--allow_empty_db`). Before a stage that calls Gemini, it checks these settings and Gemini access, as the pipeline does. It costs money: one Gemini batch request per paper, and one `GEMINI_TARGET_MATCH_MODEL` (default `gemini-3.8-flash`) call per compound use it matches. The results are in `<folder>/_pipeline_results/`. Ghostscript (`gs`), if installed, compresses PDFs over 50 MB first.

## Documentation

| Document | Contents |
|---|---|
| [docs/pipeline.md](docs/pipeline.md) | The pipeline's stages, models and settings; running `simple_batch.py`, its files and cost |
| [docs/consensus.md](docs/consensus.md) | How several runs of one compound are combined |
| [docs/prompts.md](docs/prompts.md) | The prompts: which code uses each, and their placeholders |
| [docs/database.md](docs/database.md) | The database the pipeline writes |
| [docs/glossary.md](docs/glossary.md) | Verdict, selectivity grade, PI, reviewer and other terms |
| [docs/paper_query.md](docs/paper_query.md) | Paper Query: its stages, colours, files and cost |
| [AGENTS.md](AGENTS.md) | Instructions for AI coding agents |
