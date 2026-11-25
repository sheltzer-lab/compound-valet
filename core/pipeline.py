import time
import logging
import uuid
from datetime import datetime
from core.context import Context
from core.ingestion import build_initial_context
from agents.specialists.genetics import generate_genetics_report
from agents.specialists.biochemistry import generate_biochemistry_report
from agents.specialists.biophysics import generate_biophysics_report
from agents.pi import generate_pi_report, revise_pi_report
from agents.reviewers import generate_reviewer_report
from core.analysis_schema import build_analysis_result
from core.citation_utils import resolve_paper_citations
from core.prompt_factory import PromptFactory
from core.concurrency import ContextThreadPoolExecutor
from core.utils import extract_json_from_llm_response, is_duplicate
from core.citation_manager import run_citation_manager
from core.db import save_papers_standalone, get_verified_papers, save_analysis, get_analysis
from core.identity_resolver import resolve_identity_from_report, format_for_review

logger = logging.getLogger(__name__)


def _add_reviewer_suggested_papers(drug_name: str, ctx: Context, reviewer_results: dict, scope_id: str):
    """Verify the papers the reviewers say are missing, and add the ones that pass to this
    run's papers and to ctx.

    Returns (the PI revision's prompt section listing the added papers, "" if none;
    the papers that failed verification, by reviewer).
    """
    missing_papers_text = ""
    failed_reviewer_papers = {}
    try:
        # Lazy imports
        from core.utils import extract_json_from_llm_response
        from core.citation_manager import run_citation_manager
        from core.db import save_papers_standalone, get_verified_papers

        all_missing_candidates = []

        # Extract from all reviewers (incl. the Selectivity Reviewer's proposed panel papers)
        for r_name, r_text in reviewer_results.items():
            extracted = extract_json_from_llm_response(r_text)
            if extracted and isinstance(extracted, list):
                for item in extracted:
                    # Tag source for internal debugging if needed (not strictly used yet)
                    item['_source_reviewer'] = r_name
                    all_missing_candidates.append(item)

        if all_missing_candidates:
            logger.info(f"Reviewers suggested {len(all_missing_candidates)} missing papers", extra={"drug": drug_name})

            existing_titles = set(p.get('title', '').strip().lower() for p in ctx.relevant_papers)
            candidates = []
            seen_candidate_titles = set()

            for mp in all_missing_candidates:
                t = mp.get('title', '').strip()
                t_lower = t.lower()
                if t and t_lower not in existing_titles and t_lower not in seen_candidate_titles:
                    candidates.append(mp)
                    seen_candidate_titles.add(t_lower)

            if candidates:
                verified_missing, _, _, cm_stats, _, _ = run_citation_manager(candidates, drug_name)

                for ep in cm_stats.get("excluded_papers", []):
                    src = ep.get("_source_reviewer", "Unknown")
                    if src not in failed_reviewer_papers:
                        failed_reviewer_papers[src] = []
                    failed_reviewer_papers[src].append({
                        "title": ep.get("title", "Unknown"),
                        "year": ep.get("year", "N/A"),
                        "error": ep.get("exclusion_reason", "Unknown error")
                    })

                if verified_missing:
                    logger.info(f"{len(verified_missing)} missing papers verified and added", extra={"drug": drug_name})

                    # Merge with existing (within this run's paper scope)
                    current_papers = get_verified_papers(drug_name, scope_id)

                    # Avoid hard duplicates in merge using robust check
                    final_papers = list(current_papers)
                    newly_added = []

                    for vp in verified_missing:
                        if not is_duplicate(vp, final_papers):
                            final_papers.append(vp)
                            newly_added.append(vp)

                    if newly_added:
                        save_papers_standalone(drug_name, final_papers, scope_id)

                        # Refresh context papers
                        updated_papers = get_verified_papers(drug_name, scope_id)
                        ctx.relevant_papers = updated_papers

                        # Rebuild ID map
                        new_map = {}
                        for i, paper in enumerate(updated_papers, 1):
                            pid = f"{drug_name}-Paper-{i}"
                            new_map[pid] = paper
                        ctx.verified_paper_ids = new_map

                        # Build Text List
                        list_items = []

                        for j, paper in enumerate(newly_added, 1):
                            # Find its new ID
                            p_id = "?"
                            p_title_norm = paper.get('title', '').strip().lower()

                            for pid, p in new_map.items():
                                if p.get('title', '').strip().lower() == p_title_norm:
                                    p_id = pid
                                    break

                            list_items.append(f"{j}. **{paper.get('title')}** ({paper.get('year')}) [{p_id}]")
                            link = paper.get('url', '#')
                            list_items.append(f"   Link: [{link}]({link})")

                        missing_papers_list_text = "\n".join(list_items)
                        missing_papers_text = PromptFactory.build_missing_papers_section(missing_papers_list_text)

    except Exception as e:
        logger.error(f"Failed to process missing papers: {e}")
    return missing_papers_text, failed_reviewer_papers


def run_analysis_pipeline(drug_name: str, model_name: str = None, auto_save: bool = False, overwrite: bool = False) -> dict:
    """
    Executes the full analysis pipeline for a given drug.

    Args:
        drug_name: Name of the drug to analyze.
        model_name: Name of the model to use (defaults to GEMINI_MODEL_NAME / core.llm.MODEL_NAME).
        auto_save: at the end, save the report and its papers together. A drug that is
            already in the database is not saved over unless `overwrite` is set.
        overwrite: with auto_save, save over a drug that is already in the database.

    Returns:
        A dictionary containing the full analysis result (compatible with build_analysis_result).
    """
    if not model_name:
        from core import llm as _llm
        model_name = _llm.MODEL_NAME
    start_time = time.time()


    # Decide the paper scope BEFORE ingestion, because ingestion is what writes.
    # A batch run (auto_save=True) gets a scope of its own so that the whole run
    # is scoped and then promoted in one transaction at the end: papers and
    # report land together, or a crash mid-run leaves the live drug untouched.
    # An existing drug under auto_save (without overwrite) has its report save
    # deliberately skipped (see the save branch at the end), so its papers must not go
    # live either — scoping is what keeps those two decisions consistent.
    # Consensus passes auto_save=False and isolates itself in a scratch DB, so
    # it stays unscoped here and is unaffected.
    scope_id = f"batch-{uuid.uuid4().hex[:16]}" if auto_save else None

    if not auto_save:
        # No scope, and no report will be written at the end. Ingestion is about
        # to replace this drug's live papers and nothing will update its report.
        # Consensus reaches here legitimately — it runs inside a scratch DB and
        # captures the papers itself afterwards — so only warn when there is no
        # scratch either.
        from core import scratch_db
        if scratch_db.current() is None:
            logger.warning(
                "%s: running unscoped with auto_save=False and no scratch DB. "
                "Ingestion will replace this drug's papers in the shared "
                "database while its report is left untouched. Run it with "
                "auto_save=True, or inside a scratch DB as consensus does.", drug_name)

    # A. Build Context (3-stage pipeline: search → citation manager → verified papers)
    ctx = build_initial_context(drug_name=drug_name, job_id=scope_id)
    phase1_seconds = time.time() - start_time   # retrieval + citation verification (partly API/rate-bound)

    # B. GUARANTEE: Papers are from DB (already done in build_initial_context, but verify)
    from core.db import get_verified_papers
    db_papers = get_verified_papers(drug_name, scope_id)
    assert ctx.relevant_papers == db_papers, f"Papers must be DB-sourced! Context has {len(ctx.relevant_papers)}, DB has {len(db_papers)}"


    # D. Run Specialists (Parallelized)
    logger.info(f"Running specialist analysis for {drug_name}")
    import concurrent.futures

    with ContextThreadPoolExecutor(max_workers=3) as executor:
        f_gen = executor.submit(generate_genetics_report, ctx)
        f_biochem = executor.submit(generate_biochemistry_report, ctx)
        f_biophys = executor.submit(generate_biophysics_report, ctx)

        genetics_report = f_gen.result()
        biochem_report = f_biochem.result()
        biophysics_report = f_biophys.result()
    

    pi_report_text, pi_structured_data = generate_pi_report(ctx, genetics_report, biochem_report, biophysics_report)
    draft_pi_report = pi_report_text # Capture draft for display
    draft_failed = pi_report_text.startswith("Error:")

    # E2. Resolve the structure-anchored compound identity from the (first) PI report and persist it
    # to drug_synonyms/synonym_index. Offline index + no grounding; the compound_identity stamp in
    # build_analysis_result reads it back via get_identity. Best-effort — never breaks the analysis
    # (a missing index, LLM error, or DB lock just means resolved=False for this run). The store
    # step also canonicalizes the SMILES (RDKit, offline; see core/identity_resolver.py's _store).
    identity_block = ""  # fed to the reviewers so they can audit the PI's compound ID
    try:
        _id_result = resolve_identity_from_report(
            drug_name,
            {"pi_report": pi_report_text, "structured_summary": pi_structured_data},
            model=model_name, write=True, use_search=False,
        )
        identity_block = format_for_review(_id_result)
    except Exception as e:
        logger.warning("Identity resolution skipped for %s: %s", drug_name, e)

    # F. Reviewer Stage (Parallel Reviewers)
    logger.info("Running reviewers", extra={"drug": drug_name})
    

    # What every reviewer reads: the draft report, the compound ID check, the specialist reports.
    review_inputs = {
        "drug_name": drug_name,
        "context": ctx,
        "pi_report": pi_report_text,
        "compound_identity": identity_block,
        "specialist_reports": {
            "genetics": genetics_report,
            "biochemistry": biochem_report,
            "biophysics": biophysics_report,
        },
    }

    failed_reviewer_papers = {}

    try:
        # Run Reviewers in Parallel
        # Reviewer 1: Creative (High Temp)
        # Reviewer 2: Critic (Low Temp - Default)

        reviewer_results = {}

        with ContextThreadPoolExecutor(max_workers=3) as executor:
            # Future 1: Reviewer 1 ("The Reviewer" - Neutral/Explorer) - High Temp/Top_P
            f1 = executor.submit(generate_reviewer_report, "Reviewer #1", 0.9, 0.99, "neutral", **review_inputs)

            # Future 2: Reviewer 2 ("The Critic") - Low Temp/Top_P
            f2 = executor.submit(generate_reviewer_report, "Reviewer #2", 0.05, 0.1, "critic", **review_inputs)

            # Future 3: Selectivity Reviewer - Low Temp, search on; advisory on the
            # selectivity grade only. Reads the specialist reports (panels flagged there)
            # and scours the verified sources when they didn't flag one.
            f3 = executor.submit(generate_reviewer_report, "Selectivity Reviewer", 0.05, 0.1, "selectivity", **review_inputs)

            future_names = {f1: "Reviewer #1", f2: "Reviewer #2", f3: "Selectivity Reviewer"}
            for future in concurrent.futures.as_completed(future_names):
                rname = future_names[future]
                try:
                    name, text = future.result()
                    reviewer_results[name] = text
                    logger.info(f"Reviewer {name} complete", extra={"drug": drug_name})
                except Exception as e:
                    # One reviewer failing must not abandon the others or the revision.
                    logger.error(f"Reviewer {rname} crashed: {e}", extra={"drug": drug_name})
                    reviewer_results[rname] = f"Error: {rname} failed to produce a review ({e})."

        # --- Handle Critical Missing Papers (COMBINED) ---
        missing_papers_text, failed_reviewer_papers = _add_reviewer_suggested_papers(
            drug_name, ctx, reviewer_results, scope_id)

        # G. PI Revision Stage
        logger.info("Running PI revision", extra={"drug": drug_name})

        revised = revise_pi_report(
            ctx, genetics_report, biochem_report, biophysics_report,
            drug_name=drug_name,
            reviewer_reports=reviewer_results,
            missing_papers_section=missing_papers_text,
            draft_report=draft_pi_report,
        )
        if revised is not None:
            pi_report_text, pi_structured_data = revised

    except Exception as e:
        logger.error(f"Reviewer/Revision loop failed: {e}")
        # Make sure reviewer_results exists so we don't crash next steps
        if 'reviewer_results' not in locals():
            reviewer_results = {"Reviewer #2": f"Error running Reviewer loop: {e}"}
        else:
             reviewer_results["System Error"] = f"Error running Reviewer loop: {e}"
        # Fallback to original draft (pi_report_text is already set)

    # Neither the draft nor the revision gave a report: fail the run instead of saving an error.
    if draft_failed and pi_report_text == draft_pi_report:
        from core.db import discard_job_papers
        if scope_id:
            discard_job_papers(scope_id)
        raise RuntimeError(f"No PI report for {drug_name}: the draft and the revision both failed")

    # H. Sanitize Markdown and Resolve Citations (convert [DrugName-Paper-N] to [Author, Year](URL))
    logger.info(f"Resolving citations for {drug_name}")

    from core.markdown_utils import sanitize_markdown
    
    pi_report_text = resolve_paper_citations(sanitize_markdown(pi_report_text), ctx.verified_paper_ids)
    genetics_report = resolve_paper_citations(genetics_report, ctx.verified_paper_ids)
    biochem_report = resolve_paper_citations(biochem_report, ctx.verified_paper_ids)
    biophysics_report = resolve_paper_citations(biophysics_report, ctx.verified_paper_ids)
    
    # Resolve Citations for Reviewer Reports
    # reviewer_results is {Name: Text}
    final_reviewer_reports = {}
    
    if 'reviewer_results' in locals() and reviewer_results:
        for name, text in reviewer_results.items():
            # Resolve citations
            resolved_text = resolve_paper_citations(text, ctx.verified_paper_ids)
            final_reviewer_reports[name] = resolved_text

    # Keep the draft PI report with the reviewer reports, under its own name
    if draft_pi_report:
        draft_pi_report_resolved = resolve_paper_citations(sanitize_markdown(draft_pi_report), ctx.verified_paper_ids)
        final_reviewer_reports["Original Draft"] = draft_pi_report_resolved

    # Append a note listing the reviewer-suggested papers that failed verification
    if failed_reviewer_papers:
        for r_name, failures in failed_reviewer_papers.items():
            if r_name in final_reviewer_reports:
                note = "\n\n---\n**[Admin Note] Reference Verification Failures**\n"
                note += "The following paper(s) suggested by this reviewer could not be verified and were excluded:\n"
                for f in failures:
                    note += f"- \"{f['title']}\" ({f.get('year', 'N/A')}) — Error: {f['error']}\n"
                final_reviewer_reports[r_name] += note

    end_time = time.time()
    total_duration = end_time - start_time
    
    # Usage/Cost tracking
    usage = ctx.usage_metadata
    
    # Build result
    result_data = build_analysis_result(
        drug_name=ctx.drug_name,
        context=ctx,
        report_text=pi_report_text,
        specialist_reports={
            "genetics": genetics_report,
            "biochemistry": biochem_report,
            "biophysics": biophysics_report
        },
        usage=usage,
        model_name=model_name,
        time=total_duration,
        structured_data=pi_structured_data,
        reviewer_reports=final_reviewer_reports,
    )
    # Stage split for throughput telemetry: phase-1 (retrieval + citation verification,
    # partly external-API/rate-bound) vs the remainder (specialist/reviewer/PI LLM work).
    result_data["phase1_seconds"] = round(phase1_seconds, 1)

    # SAVE TO DATABASE (Conditional)
    # If auto_save is False (consensus runs), skip: the caller saves the run it picks.
    # If auto_save is True (single-run batch), save new drugs automatically.
    #
    # Whichever branch runs, the papers currently belong to scope_id and
    # verified_papers has not been touched. Promoting writes papers and report
    # together; not promoting must therefore DISCARD that run's papers, never
    # leave them half-applied.
    from core.db import promote_job_papers, discard_job_papers
    if auto_save:
        existing_analysis = get_analysis(drug_name)
        if not existing_analysis or overwrite:
            promote_job_papers(drug_name, result_data, job_id=scope_id)
            logger.info(f"{'Existing' if existing_analysis else 'New'} drug {drug_name}: Auto-saved to DB.")
        else:
            # Already in the database: the report is deliberately not written, so the
            # papers must not be either. Before this scoping they WERE written,
            # at ingestion, leaving the drug citing papers it no longer had.
            if scope_id:
                discard_job_papers(scope_id)
            logger.info(f"Existing drug {drug_name}: Skipped auto-save (already in the database).")
    else:
        # Consensus: the caller takes the papers from its scratch database and
        # saves the run it picks.
        logger.info(f"Drug {drug_name}: auto_save=False, skipping DB save.")

    return result_data
