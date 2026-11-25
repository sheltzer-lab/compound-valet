# Paper Query

Paper Query reads a folder of research papers (PDFs) and checks the compounds they use. For each paper it finds every compound the authors use, and which of them they use for a stated target. It matches each of those compounds to a compound in the VALET database, and checks the paper's target against the compound's report. Each use gets a colour (below).

`scripts/batch_paper_pipeline.py` is the source of truth for the stages. If this page and the code disagree, the code is right; fix the page.

```bash
python scripts/batch_paper_pipeline.py <folder of PDFs> --year_cutoff 2000
```

## What it needs

- **PDF file names** of the form `First author et al. - YYYY - Title.pdf`. Files that do not match are skipped. So are papers named with a year before `--year_cutoff` (default 2024).
- **A populated VALET database**, the one the pipeline writes (`DATABASE_URL`, default `data/compound_valet.db`). Compounds are matched against its reports. Before submitting a batch job, Paper Query stops if `analysis_results` has no rows (pass `--allow_empty_db` to override).
- **Gemini on Vertex AI and a Cloud Storage bucket.** The PDFs are uploaded to the bucket and read in one Gemini batch job. Set `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` and `GCS_BUCKET_NAME` (README: Settings). `GEMINI_MODEL_NAME` sets the extraction model (default `gemini-3.1-pro-preview`), and `GEMINI_TARGET_MATCH_MODEL` sets the target-check model (default `gemini-3.8-flash`). Before a stage that calls Gemini (not with `--dry_run`), Paper Query checks these settings, and that both models can be reached; it stops with a message if not (as the pipeline does: [pipeline.md](pipeline.md#running-compounds-simple_batchpy)).
- **Ghostscript** (`gs`), optional. A PDF over 50 MB is compressed with it before upload; without it, pypdf's lossless compression is tried.

## Stages

```
folder of PDFs
  │
  ▼  1. File names: author, year, title; year filter; duplicate downloads removed
  ▼  2. Paper type, by title: Semantic Scholar, else Crossref; reviews, editorials, letters,
  │     meta-analyses, case reports, news, peer reviews, datasets and similar are left out.
  │     A paper found in neither is kept. Missing journals from Crossref and Europe PMC
  ▼  3. Gemini batch: each PDF with prompts/paper_query_prompt.md -> the compounds the paper uses
  ▼     Match and target check, for each compound used for a stated target (below)
  ▼  4. Filter: papers with fewer than --min_targeted_drugs targeted compounds (default 0) are left out
  ▼  5. Reports: CSV files and an HTML page; journals still missing are looked up again, also in PubMed
results in <folder>/_pipeline_results/
```

Step 3 has three resumable parts: submit the batch job (`submit`), poll it (`poll`), and download and parse the answers (`extract`). Then each targeted compound is matched and checked (`verify`).

### 3. Extraction

Gemini returns, for each paper, the compounds used for a stated target (name, other names, the paper's target, the human gene, a one-line description of the use, and the sentence that states the target) and the other compounds it uses (`additional_drugs`). The request uses temperature 0.1, JSON output, and an output limit of 65,536 tokens.

A compound counts as targeted only when the paper names a target and a sentence that states it. Compounds without them, and the reagents in `ALWAYS_NON_TARGETED` (`core/paper_query_engine.py`: selection antibiotics, inducers, vehicle and assay reagents, and compounds papers use as general tools), are listed as not targeted. The names in `BATCH_EXCLUDED_DRUGS` are dropped: generic labels such as "Compound 1", and some named agents, such as antibody–drug conjugates, Z-DEVD-FMK, DNase, Anakinra and Vancomycin.

### Match

`core/paper_drug_index.py` finds the database compound for the paper's name: by our compound names, their synonyms, and the name with or without a salt word. (+), (−) and rac stay part of the name. When several database compounds fit, none is chosen, and the candidates are listed as possible matches. One exception: when they are one compound held as a salt and as its free form, the salt entry is taken, if its structure is that compound with an ordinary counter-ion and its name says salt. Generic labels ("compound 9", "inhibitor") are never matched, apart from "Compound C" (the usual name for dorsomorphin) and codes whose digit groups are separated, such as "666-15".

### Target check

For a matched compound, `gemini-3.8-flash` compares the paper's target with the compound's report (target, genes, selectivity and an excerpt), with `prompts/paper_target_match_prompt.md`. The prompt also accepts an adjacent step in the same pathway (ERK for a MEK inhibitor). One call per matched compound use.

### Colours

| Colour | Meaning |
|---|---|
| 🟢 | The report is VERIFIED, the target check matched, and the compound is selective or moderately selective |
| 🟡 | The report is VERIFIED and the target check matched; the compound is promiscuous, or its selectivity is unknown |
| 🔴 | Unverified: the report is UNVERIFIED, or the target check found that the paper's target is not the compound's verified target |
| ⚪ | Not in the database (or several database compounds fit) |
| ⚠️ | The target check failed. It runs before the verdict is read, so a failed check gives ⚠️ even when the report is UNVERIFIED. The stage 5 CSV files count ⚠️ uses with the not-in-database ones |

For 🟡 and 🔴 uses, Paper Query lists **alternates**: verified, selective database compounds whose genes include the paper's gene.

## Output

`<folder>/pipeline_state.json` holds the stage reached and the Cloud Storage address of each uploaded PDF. The results are in `<folder>/_pipeline_results/`:

| File | Contents |
|---|---|
| `stage1_parsed.json`, `stage2_s2_results.json` | Papers after stages 1 and 2 |
| `stage3_gemini_parsed.json` | Gemini's extraction, per paper |
| `stage3_parsed.json` | Per paper: each targeted compound with its match, colour, selectivity and alternates; the compounds not targeted |
| `stage4_filtered.json` | The same, for the papers that pass stage 4 |
| `stage5_reports/` | CSV files per colour, compound and gene frequencies (`drug_freq_all_targeted.csv`: the number of papers that use each compound), and `pipeline_report.html` |

## Running parts again

The run is resumable: it starts at the stage in `<folder>/pipeline_state.json`. `--dry_run` goes through the stages without calling Gemini, uploading, or writing any files, and saves no progress. `--force_stage <stage>` starts again from an earlier stage (`parse`, `s2`, `submit`, `poll`, `extract`, `verify`, `filter`, `report`).

- After the database changes, `--force_stage verify` matches and checks every compound again, without a new extraction. That is one target-check call per matched compound use.
- The submit stage skips every paper already in `stage3_gemini_parsed.json`. To extract a paper again, remove its entry from that file (or the file) as well.
- `pipeline_state.json` keeps the Cloud Storage address of each uploaded PDF, under its file name, and submit uses it instead of uploading again. If you change a PDF and keep its file name, remove its entry from `files` in `pipeline_state.json` too.

## Cost

The extraction is one Gemini batch request per paper; batch requests cost less than single calls. In our runs, 302 papers took about 5.1 million input and 1.0 million output tokens with `gemini-3.1-pro-preview`, about $11 at batch list prices (estimated from the token counts). The target checks add one `gemini-3.8-flash` call per matched compound use.
