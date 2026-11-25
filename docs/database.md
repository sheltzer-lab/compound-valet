# Database

The pipeline writes to one SQLite database: `data/compound_valet.db`, unless `DATABASE_URL` names another SQLite file (for example `sqlite:///data/other.db`). Any other kind of URL stops the run with an error. `core/db.py` defines the tables and creates any that are missing. It is the source of truth for this page.

| Table | One row per | Written by |
|---|---|---|
| `analysis_results` | Compound: its saved report | `promote_job_papers`, at the end of a run (single-run batch) or for the run a consensus batch saves |
| `verified_papers` | Paper a saved report cites | The citation check, through `promote_job_papers` |
| `job_scoped_verified_papers` | Paper of a single run whose report is not saved yet | The citation check during a run. Emptied for the run when it is saved or discarded |
| `drug_targets` | Compound and target gene | `promote_job_papers` (from the target gene list) |
| `drug_synonyms` | Compound: its resolved structure and registry IDs | Identity resolution (for a consensus batch, the saved run's identity) |
| `synonym_index` | Alias of a compound (brand name, code, salt name, registry ID) | Identity resolution (as above) |

## `analysis_results`

| Column | Contents |
|---|---|
| `drug_name` | The compound name (unique) |
| `verdict` | The verdict as the PI's JSON block gives it: `VERIFIED` or `UNVERIFIED`. Without a JSON verdict, the text of the report's Verdict line; "Unknown" when there is none |
| `target` | The target, as text (for example "Thymidylate synthase") |
| `target_gene_names_list` | The target's HGNC gene symbols (JSON list) |
| `selectivity_grade` | `SELECTIVE`, `MODERATELY SELECTIVE`, `PROMISCUOUS`, `UNKNOWN`, or empty (the PI leaves it out where it does not apply) |
| `mechanism` | Mechanism class: Inhibitor, Activator, Modulator, Degrader, Multiple mechanisms, Other, or Unknown (always Unknown for an UNVERIFIED compound) |
| `pi_report` | The final report, in markdown, with citation links |
| `genetics_report`, `biochem_report`, `biophysics_report` | The specialist reports |
| `reviewer_reports` | JSON: the PI's draft (`Original Draft`) and each reviewer's review |
| `structured_summary` | JSON: the fields of the PI's JSON block, the compound's identity (`compound_identity`), and the run's papers without their links (`harvested_papers`) |
| `raw_context` | The papers as the specialists saw them (markdown) |
| `system_context` | A one-line summary of the run context |
| `citation_score`, `citation_stats` | The citation check's results |
| `usage_stats` | The run's tokens (input, output, thinking, cached) and Google Search queries, with the search queries per stage |
| `cost`, `time` | Estimated cost in US dollars ([pipeline.md](pipeline.md#cost)), seconds |
| `timestamp` | When the report was saved (UTC) |
| `corrected_pi_report` | Not filled by this code |

`target` (text) and `target_gene_names_list` (gene symbols) are different fields. Do not use one for the other.

## Papers

`verified_papers` and `job_scoped_verified_papers` have the same columns: title, authors, year, link, citation text, DOI, and the retraction status from Crossref (`retracted` is true, false, or empty when unknown).

Until its report is saved, a run's papers are kept apart from the saved ones: a single run keeps them in `job_scoped_verified_papers`, under the run's ID; a consensus run keeps them in its own temporary database. `promote_job_papers` then writes the report and its papers to `verified_papers` in one transaction. So a report and the papers it cites always come from the same run.
