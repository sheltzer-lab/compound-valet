# Prompts

The 17 prompts are the files in `prompts/`: 15 for the pipeline and 2 for Paper Query. The code fills in the `{{...}}` placeholders; `core/prompt_factory.py` (`PromptFactory`) builds most of the pipeline's prompts. Changing a prompt changes the reports. A new `{{placeholder}}` must also be filled in the code. `PromptFactory.validate_no_unreplaced_placeholders` lists the placeholders left in a prompt; the tests use it, the pipeline does not.

## Pipeline

| File | Used by | What it asks for |
|---|---|---|
| `literature_search_simple.md` | `core/ingestion.py`, once per search angle | Papers about the compound, from a grounded Google search |
| `genetics_prompt.md` | `agents/specialists/genetics.py` | The genetic evidence for the target |
| `biochemistry_prompt.md` | `agents/specialists/biochemistry.py` | The binding and activity evidence |
| `biophysics_prompt.md` | `agents/specialists/biophysics.py` | The direct-binding and structural evidence |
| `pi_persona.md` | Included in the PI and reviewer prompts (`{{pi_persona}}`) | The PI's role and inputs (the compound, a run summary, the three specialist reports, the verified papers) and its standards: how to weigh evidence, the evidence bar per target class, when to say UNVERIFIED |
| `pi_report_format.md` | Included in the PI prompts (`{{pi_report_format}}`) | The report's sections and the JSON block (verdict, target, target genes, selectivity grade, mechanism class) |
| `pi_draft.md` | `agents/pi.py::generate_pi_report` | The draft report. The file is `pi_persona.md` and `pi_report_format.md` together |
| `identity_candidate_pick.md` | `core/identity_resolver.py` | Which PubChem structure the compound name refers to |
| `identity_synonym_filter.md` | `core/identity_resolver.py` | Which PubChem synonyms are true names of that structure |
| `reviewer_neutral.md` | `agents/reviewers.py` (Reviewer #1) | Other readings of the evidence, and papers the draft missed |
| `reviewer_critic.md` | `agents/reviewers.py` (Reviewer #2) | Errors in the draft |
| `reviewer_selectivity.md` | `agents/reviewers.py` (Selectivity Reviewer) | Whether the selectivity grade is right, using the specialists' reports |
| `missing_papers_section.md` | `core/pipeline.py::_add_reviewer_suggested_papers` | Lists the papers added from the reviewers' suggestions, for the revision |
| `pi_revision_prompt.md` | `agents/pi.py::revise_pi_report` | The final report, from the draft, the three reviews and the added papers |
| `target_match_prompt.md` | `core/target_matching.py` (consensus) | Whether two runs name the same target |

## Paper Query

| File | Used by | What it asks for |
|---|---|---|
| `paper_query_prompt.md` | `core/paper_query_engine.py`, sent with each PDF by `scripts/batch_paper_pipeline.py` | The compounds the paper uses, and for which stated target ([paper_query.md](paper_query.md)) |
| `paper_target_match_prompt.md` | `core/target_matching.py`, called by `core/paper_query_engine.py` | Whether the paper's target is the target in the compound's report |

## Placeholders

| Placeholder | Filled with |
|---|---|
| `{{drug_name}}` | The compound name |
| `{{verified_sources}}` | The verified papers with their IDs (`<compound>-Paper-N`), by `core/prompt_builder.py::inject_verified_sources` |
| `{{agent_context}}` | The specialist's view of the run context (`Context.get_agent_context`) |
| `{{context_summary}}` | A short run summary (`Context.get_minimal_context`) |
| `{{genetics_report}}`, `{{biochem_report}}`, `{{biophysics_report}}` | The specialist reports (PI prompts) |
| `{{specialist_reports}}` | All three specialist reports (Selectivity Reviewer) |
| `{{pi_report}}` | The PI's draft (reviewers) |
| `{{compound_identity}}` | The identity resolution summary (Reviewers #1 and #2) |
| `{{initial_pi_report}}` | The PI's draft (revision) |
| `{{reviewer1_feedback}}`, `{{reviewer2_feedback}}`, `{{reviewer_selectivity_feedback}}` | The three reviews (revision) |
| `{{missing_papers_section}}` | The filled `missing_papers_section.md`, or nothing (revision) |
| `{{missing_papers_list}}` | The added papers (`missing_papers_section.md`) |

The reviewer prompts include `pi_persona.md`, but `PromptFactory.build_reviewer_prompt` fills only the persona's `{{context_summary}}` and `{{verified_sources}}`. Its `{{drug_name}}`, `{{genetics_report}}`, `{{biochem_report}}` and `{{biophysics_report}}` reach Gemini as they are written in the file.

The other prompts are filled by their own code:

- `identity_candidate_pick.md` and `identity_synonym_filter.md` have Python `{...}` fields, which `core/identity_resolver.py` fills with `str.format`. Literal braces in these files must be doubled (`{{`, `}}`).
- `target_match_prompt.md` and `paper_target_match_prompt.md` have no placeholders: `core/target_matching.py` adds the compounds to compare after the prompt text.
- `paper_query_prompt.md` has no placeholders: it goes to Gemini with the PDF.
