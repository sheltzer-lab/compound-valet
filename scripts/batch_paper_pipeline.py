"""
Multi-stage batch paper analysis pipeline.

Processes a directory of PDFs through 5 stages:
  1. Filename Parse + Year Filter
  2. Semantic Scholar Lookup + Type Filter
  3. PDF Compress + Gemini Batch
  4. Post-Gemini Filtering
  5. Report Generation (CSV + HTML)

Usage:
    python scripts/batch_paper_pipeline.py <folder> [options]

Options:
    --poll_interval      Seconds between batch status checks (default: 60)
    --year_cutoff        Exclude papers before this year (default: 2024)
    --min_targeted_drugs Minimum targeted drugs per paper (default: 0)
    --force_stage        Re-run from: parse|s2|submit|poll|extract|verify|filter|report
    --dry_run            Show what would happen without executing
"""

import os
import sys
import re
import json
import time
import glob
import asyncio
import logging
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import aiohttp

# Ensure project root is in path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core.async_utils import submit_batch_job, check_job_status, download_batch_results
from core.utils import extract_json_from_llm_response
from core.paper_query_engine import PaperQueryEngine, ALWAYS_NON_TARGETED
from core.db import (
    AnalysisResult,
    DATABASE_URL,
    get_verified_selective_drugs_by_target,
    init_db,
    normalize_key,
)
from core.paper_drug_index import DrugIndex
from core.references.clients import SemanticScholarClient, CrossRefClient, BaseClient, GlobalRateLimiter
from core.references.utils import clean_title_for_search, normalize_text

EUROPEPMC_GLOBAL_LIMITER = GlobalRateLimiter(requests_per_second=5.0, name="EuropePMC")

# DOI prefix → journal name for supplementary/data-deposit DOIs that lack
# container-title in CrossRef metadata.
DOI_JOURNAL_MAP = {
    "10.1158/0008-5472": "Cancer Research",
    "10.1158/2159-8290": "Cancer Discovery",
    "10.1158/1078-0432": "Clinical Cancer Research",
    "10.1158/1541-7786": "Molecular Cancer Research",
    "10.1158/2767-9764": "Cancer Research Communications",
    "10.1073/pnas": "Proceedings of the National Academy of Sciences",
    "10.1038/s41467": "Nature Communications",
    "10.1038/s41586": "Nature",
    "10.1038/s41588": "Nature Genetics",
    "10.1038/s41591": "Nature Medicine",
    "10.1038/s41556": "Nature Cell Biology",
    "10.1038/s41590": "Nature Immunology",
    "10.1038/s41587": "Nature Biotechnology",
    "10.1038/s41589": "Nature Chemical Biology",
    "10.1038/s41592": "Nature Methods",
    "10.1038/s43018": "Nature Cancer",
}
from core.model_config import GCS_BUCKET_NAME, USE_VERTEX_AI
from core.storage import upload_to_gcs
from core.client_factory import check_gemini_setup, GeminiSetupError
from core.llm import MODEL_NAME
from core.target_matching import TARGET_MATCH_MODEL

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Batch-specific drug exclusions — generic/unnamed compounds and reagents that
# clutter the batch report.
BATCH_EXCLUDED_DRUGS = {
    normalize_key(d) for d in [
        "Compound 1", "Compound 2", "Compound 3", "Compound 4", "Compound 5",
        "Compound 6", "Compound 7", "Compound 8", "Compound 9",
        "Z-DEVD-FMK", "Trastuzumab deruxtecan",
        "DNase I", "DNase",
        "CpG", "Anakinra", "Enfortumab vedotin", "Trastuzumab emtansine",
        "Pam3CSK4", "TRAIL", "Sacituzumab govitecan", "Vancomycin",
        "B/B Homodimerizer",
    ]
}

# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------

STAGE_ORDER = ["init", "parsed", "s2_checked", "submitted", "polled", "gemini_parsed", "extracted", "filtered", "reported"]

# Map force_stage CLI names to internal state names
FORCE_STAGE_MAP = {
    "parse": "init",
    "s2": "parsed",
    "submit": "s2_checked",
    "poll": "submitted",
    "extract": "polled",
    "verify": "gemini_parsed",
    "filter": "extracted",
    "report": "filtered",
}

RESULTS_DIR = "_pipeline_results"
STATE_FILE = "pipeline_state.json"


def results_dir(folder: str) -> str:
    return os.path.join(folder, RESULTS_DIR)


def load_state(folder: str) -> dict:
    path = os.path.join(folder, STATE_FILE)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"stage": "init", "files": {}, "job_id": None}


def save_state(folder: str, state: dict):
    path = os.path.join(folder, STATE_FILE)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def stage_index(stage: str) -> int:
    return STAGE_ORDER.index(stage)


# ---------------------------------------------------------------------------
# Stage 1: Filename Parse + Year Filter
# ---------------------------------------------------------------------------

FILENAME_RE = re.compile(r"^(.+?)\s*-\s*(\d{4})\s*-\s*(.+)\.pdf$", re.IGNORECASE)


def stage1_parse(folder: str, year_cutoff: int, dry_run: bool) -> list:
    """Parse PDF filenames and filter by year."""
    pdf_files = sorted(glob.glob(os.path.join(folder, "*.pdf")))
    if not pdf_files:
        logger.error("No PDF files found in %s", folder)
        return []

    parsed = []
    rejected = []

    for pdf_path in pdf_files:
        fname = os.path.basename(pdf_path)
        m = FILENAME_RE.match(fname)
        if not m:
            rejected.append({"filename": fname, "reason": "filename_no_match"})
            logger.warning("Filename does not match pattern: %s", fname)
            continue

        author = m.group(1).strip()
        year = int(m.group(2))
        title = m.group(3).strip()
        size_mb = os.path.getsize(pdf_path) / (1024 * 1024)

        if year < year_cutoff:
            rejected.append({"filename": fname, "reason": f"year_{year}_below_{year_cutoff}"})
            logger.info("Skipping %s (year %d < %d)", fname, year, year_cutoff)
            continue

        parsed.append({
            "filename": fname,
            "author": author,
            "year": year,
            "title": title,
            "file_path": pdf_path,
            "file_size_mb": round(size_mb, 2),
        })

    # --- Deduplicate browser re-downloads (e.g. "paper 2.pdf", "paper (3).pdf") ---
    _BROWSER_SUFFIX_RE = re.compile(r'(\s+\d+|\s+\(\d+\))$')

    def _normalize_title_for_dedup(t: str) -> str:
        return _BROWSER_SUFFIX_RE.sub('', t).strip().lower()

    groups: dict[tuple, list] = {}
    for paper in parsed:
        key = (paper["author"].lower(), paper["year"],
               _normalize_title_for_dedup(paper["title"]))
        groups.setdefault(key, []).append(paper)

    deduped = []
    for key, papers in groups.items():
        if len(papers) == 1:
            deduped.append(papers[0])
            continue
        # Prefer the file without a browser suffix; otherwise lowest suffix number
        def _sort_key(p):
            m_suf = _BROWSER_SUFFIX_RE.search(p["title"])
            if m_suf is None:
                return (0, 0)  # original — best
            digits = re.search(r'\d+', m_suf.group())
            return (1, int(digits.group()) if digits else 999)
        papers.sort(key=_sort_key)
        kept = papers[0]
        deduped.append(kept)
        for dup in papers[1:]:
            rejected.append({
                "filename": dup["filename"],
                "reason": f"duplicate_of_{kept['filename']}",
            })
            logger.info("Dedup: dropping %s (duplicate of %s)",
                        dup["filename"], kept["filename"])

    if len(deduped) < len(parsed):
        logger.info("Dedup removed %d duplicate(s): %d → %d papers",
                     len(parsed) - len(deduped), len(parsed), len(deduped))
    parsed = deduped

    if not dry_run:
        out_dir = results_dir(folder)
        os.makedirs(out_dir, exist_ok=True)

        out_path = os.path.join(out_dir, "stage1_parsed.json")
        with open(out_path, "w") as f:
            json.dump(parsed, f, indent=2)

        if rejected:
            rej_path = os.path.join(out_dir, "stage1_rejected.json")
            with open(rej_path, "w") as f:
                json.dump(rejected, f, indent=2)

    logger.info(
        "Stage 1 complete: %d papers passed, %d rejected", len(parsed), len(rejected)
    )
    return parsed


# ---------------------------------------------------------------------------
# Stage 2: Semantic Scholar Lookup + Type Filter
# ---------------------------------------------------------------------------

REJECTED_PUB_TYPES = {"Review", "Editorial", "LettersAndComments", "MetaAnalysis", "CaseReport", "News"}
CROSSREF_REJECT_SET = {"peer-review", "book-review", "monograph", "reference-entry", "dataset", "component"}

SIMILARITY_THRESHOLD = 0.8

# Prefixes that CrossRef prepends to conference abstracts, withdrawn notices,
# and data deposits.  Stripped before title similarity scoring so they don't
# inflate scores for non-primary publications.
_CR_TITLE_PREFIX_RE = re.compile(
    r"^("
    r"abstract\s+[a-z0-9\-]+\s*:\s*"       # "Abstract B017: ..."
    r"|withdrawn\s*:\s*\d*[a-z]*\s*"        # "WITHDRAWN: 567P ..."
    r"|data\s+from\s+"                      # "Data from ..."
    r"|supplementary\s+(data\s+)?from\s+"   # "Supplementary Data from ..."
    r")",
    re.IGNORECASE,
)


def _title_similarity(t1: str, t2: str) -> float:
    if not t1 or not t2:
        return 0.0
    n1, n2 = normalize_text(t1), normalize_text(t2)
    full_score = SequenceMatcher(None, n1, n2).ratio()
    if full_score >= SIMILARITY_THRESHOLD:
        return full_score
    # Prefix fallback: truncated filenames may cut off the end of long titles.
    # Compare the shorter string against just the prefix of the longer one.
    # Require the shorter string to be at least 50% of the longer to avoid
    # false positives on very short prefixes.
    short, long = (n1, n2) if len(n1) <= len(n2) else (n2, n1)
    if len(short) < len(long) * 0.5:
        return full_score
    prefix_score = SequenceMatcher(None, short, long[:len(short)]).ratio()
    return max(full_score, prefix_score)


def _find_best_s2_match(title: str, candidates: list) -> dict | None:
    best = None
    best_score = 0.0
    for cand in candidates:
        score = _title_similarity(title, cand.get("title", ""))
        if score > best_score:
            best_score = score
            best = cand
    if best_score >= SIMILARITY_THRESHOLD:
        return best
    return None


def _is_rejected_pub_type(pub_types: list | None) -> str | None:
    """Return the first rejected type found, or None if OK."""
    if not pub_types:
        return None
    for pt in pub_types:
        if pt in REJECTED_PUB_TYPES:
            return pt
    return None


async def _s2_lookup(papers: list) -> list:
    """Look up papers in Semantic Scholar."""
    client = SemanticScholarClient()
    titles = [p["title"] for p in papers]
    timeout = aiohttp.ClientTimeout(total=120, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await client.search_paper_bulk(titles, session)
    return results


async def _crossref_lookup_batch(papers: list, concurrency: int = 5) -> dict[str, list]:
    """Look up multiple papers in CrossRef concurrently."""
    client = CrossRefClient()
    results = {}
    completed = 0
    total = len(papers)

    semaphore = asyncio.Semaphore(concurrency)

    async def lookup_one(paper, session):
        nonlocal completed
        async with semaphore:
            cleaned = clean_title_for_search(paper["title"])
            try:
                cr_results = await client.search_works(cleaned, session)
                results[paper["filename"]] = cr_results
            except Exception as e:
                logger.warning("CrossRef lookup failed for %s: %s", paper["filename"], e)
                results[paper["filename"]] = []
            completed += 1
            if completed % 10 == 0 or completed == total:
                logger.info("CrossRef progress: %d/%d looked up", completed, total)

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [lookup_one(paper, session) for paper in papers]
        await asyncio.gather(*tasks)

    return results


async def _crossref_venue_lookup(
    papers: list,
    use_year: bool = True,
    type_filter: str | None = None,
    concurrency: int = 5,
    label: str = "CrossRef venue",
) -> dict[str, dict]:
    """Look up papers in CrossRef to resolve venue/journal.

    Returns {filename: {"venue": str, "score": float, "doi": str|None}}.
    """
    client = CrossRefClient()
    matches: dict[str, dict] = {}
    completed = 0
    total = len(papers)

    semaphore = asyncio.Semaphore(concurrency)

    async def lookup_one(paper: dict, session: aiohttp.ClientSession):
        nonlocal completed
        async with semaphore:
            cleaned = clean_title_for_search(paper["title"])
            year = int(paper["year"]) if use_year and paper.get("year") else None
            try:
                cr_results = await client.search_works(
                    cleaned, session, year=year, type_filter=type_filter,
                )
            except Exception as e:
                logger.warning("%s lookup failed for %s: %s", label, paper["filename"], e)
                cr_results = []

            best_match = None
            best_score = 0.0
            for cr in cr_results:
                cr_title = cr.get("title", [""])[0] if cr.get("title") else ""
                cleaned_cr_title = _CR_TITLE_PREFIX_RE.sub("", cr_title).strip()
                score = _title_similarity(paper["title"], cleaned_cr_title)
                if score >= SIMILARITY_THRESHOLD and score > best_score:
                    best_score = score
                    best_match = cr

            if best_match:
                container = best_match.get("container-title", [])
                venue = container[0] if container else None
                if venue:
                    match_info = {"venue": venue, "score": best_score}
                    cr_doi = best_match.get("DOI")
                    if cr_doi:
                        match_info["doi"] = cr_doi
                    cr_url = best_match.get("URL")
                    if cr_url:
                        match_info["url"] = cr_url
                    matches[paper["filename"]] = match_info

            completed += 1
            if completed % 25 == 0 or completed == total:
                logger.info("%s progress: %d/%d looked up, %d matched so far",
                            label, completed, total, len(matches))

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [lookup_one(p, session) for p in papers]
        await asyncio.gather(*tasks)

    return matches


async def _europepmc_venue_lookup(
    papers: list,
    concurrency: int = 3,
) -> dict[str, dict]:
    """Search EuropePMC REST API for papers missing venue.

    Returns {filename: {"venue": str, "score": float, "doi": str|None}}.
    """
    base_client = BaseClient()
    matches: dict[str, dict] = {}
    completed = 0
    total = len(papers)

    semaphore = asyncio.Semaphore(concurrency)

    async def lookup_one(paper: dict, session: aiohttp.ClientSession):
        nonlocal completed
        async with semaphore:
            title = paper["title"]
            cleaned = clean_title_for_search(title)
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            params = {
                "query": f'TITLE:"{cleaned}"',
                "format": "json",
                "pageSize": 5,
                "resultType": "core",
            }
            try:
                data, status = await base_client._make_request(
                    session, url, params=params, limiter=EUROPEPMC_GLOBAL_LIMITER,
                )
            except Exception as e:
                logger.warning("EuropePMC lookup failed for %s: %s", paper["filename"], e)
                data = None

            if data:
                results = data.get("resultList", {}).get("result", [])
                best_match = None
                best_score = 0.0
                for r in results:
                    epmc_title = r.get("title", "")
                    score = _title_similarity(title, epmc_title)
                    if score >= SIMILARITY_THRESHOLD and score > best_score:
                        best_score = score
                        best_match = r

                if best_match:
                    venue = (best_match.get("journalTitle")
                             or (best_match.get("journalInfo") or {}).get("journal", {}).get("title"))
                    if venue:
                        match_info = {"venue": venue, "score": best_score}
                        epmc_doi = best_match.get("doi")
                        if epmc_doi:
                            match_info["doi"] = epmc_doi
                        matches[paper["filename"]] = match_info

            completed += 1
            if completed % 10 == 0 or completed == total:
                logger.info("EuropePMC progress: %d/%d looked up, %d matched so far",
                            completed, total, len(matches))

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [lookup_one(p, session) for p in papers]
        await asyncio.gather(*tasks)

    return matches


async def _pubmed_venue_lookup(
    papers: list,
    concurrency: int = 3,
) -> dict[str, str]:
    """Search PubMed by title to resolve journal names.

    Returns {filename: journal_name}.
    """
    matches: dict[str, str] = {}
    completed = 0
    total = len(papers)

    semaphore = asyncio.Semaphore(concurrency)

    async def lookup_one(paper: dict, session: aiohttp.ClientSession):
        nonlocal completed
        async with semaphore:
            title = paper.get("title", "")
            cleaned = clean_title_for_search(title)
            search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            params = {"db": "pubmed", "term": f'{cleaned}[Title]', "retmode": "json", "retmax": 3}
            try:
                async with session.get(search_url, params=params) as resp:
                    data = await resp.json()
                ids = data.get("esearchresult", {}).get("idlist", [])
                if ids:
                    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                    fparams = {"db": "pubmed", "id": ",".join(ids[:3]), "retmode": "json"}
                    async with session.get(fetch_url, params=fparams) as resp2:
                        fdata = await resp2.json()
                    for pmid in ids[:3]:
                        article = fdata.get("result", {}).get(pmid, {})
                        pm_title = article.get("title", "")
                        journal = article.get("fulljournalname") or article.get("source", "")
                        score = _title_similarity(title, pm_title)
                        if score >= 0.75 and journal:
                            matches[paper["filename"]] = journal
                            break
            except Exception as e:
                logger.warning("PubMed lookup failed for %s: %s", paper["filename"], e)

            completed += 1
            if completed % 10 == 0 or completed == total:
                logger.info("PubMed progress: %d/%d looked up, %d matched so far",
                            completed, total, len(matches))
            await asyncio.sleep(0.35)  # NCBI rate limit: ~3 RPS without API key

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [lookup_one(p, session) for p in papers]
        await asyncio.gather(*tasks)

    return matches


def stage2_s2_lookup(folder: str, papers: list, dry_run: bool) -> list:
    """Look up papers in S2, filter by publication type."""

    if dry_run:
        logger.info("[DRY RUN] Would look up %d papers in Semantic Scholar", len(papers))
        return papers

    # Bulk S2 search
    s2_results = asyncio.run(_s2_lookup(papers))
    logger.info("S2 bulk search returned %d candidates", len(s2_results))

    passed = []
    rejected = []
    s2_missed = []

    # First pass: match against S2 results
    for paper in papers:
        match = _find_best_s2_match(paper["title"], s2_results)

        if match:
            pub_types = match.get("publicationTypes") or []
            venue = match.get("publicationVenue") or {}
            rejected_type = _is_rejected_pub_type(pub_types)

            if rejected_type:
                rejected.append({
                    **paper,
                    "reason": f"s2_type_{rejected_type}",
                    "s2_pub_types": pub_types,
                })
                logger.info("Rejected %s: S2 type=%s", paper["filename"], rejected_type)
                continue

            # Enrich with S2 metadata
            paper["s2_title"] = match.get("title")
            paper["s2_year"] = match.get("year")
            paper["s2_pub_types"] = pub_types
            # Prefer journal.name (reliably populated) over publicationVenue (entity-linked, often null)
            journal = match.get("journal") or {}
            journal_name = journal.get("name") if isinstance(journal, dict) else None
            venue_name = venue.get("name") if isinstance(venue, dict) else str(venue) if venue else None
            paper["s2_venue"] = journal_name or venue_name
            paper["s2_url"] = match.get("url")
            paper["s2_external_ids"] = match.get("externalIds")
            passed.append(paper)
        else:
            s2_missed.append(paper)

    logger.info(
        "S2 matching: %d matched, %d rejected, %d need CrossRef fallback (of %d total)",
        len(passed), len(rejected), len(s2_missed), len(papers),
    )

    # Second pass: concurrent CrossRef lookup for S2 misses
    if s2_missed:
        cr_results_map = asyncio.run(_crossref_lookup_batch(s2_missed))

        for paper in s2_missed:
            cr_results = cr_results_map.get(paper["filename"], [])

            # Pick the highest-similarity match, stripping common
            # academic prefixes ("Abstract B017:", "WITHDRAWN: 567P",
            # "Data from", "Supplementary Data from") that inflate
            # scores for conference abstracts / data deposits.
            cr_match = None
            best_score = 0.0
            for cr in cr_results:
                cr_title = cr.get("title", [""])[0] if cr.get("title") else ""
                cleaned_cr_title = _CR_TITLE_PREFIX_RE.sub("", cr_title).strip()
                score = _title_similarity(paper["title"], cleaned_cr_title)
                if score >= SIMILARITY_THRESHOLD and score > best_score:
                    best_score = score
                    cr_match = cr

            if cr_match:
                cr_type = cr_match.get("type", "")
                if cr_type in CROSSREF_REJECT_SET:
                    rejected.append({
                        **paper,
                        "reason": f"crossref_type_{cr_type}",
                    })
                    logger.info("Rejected %s: CrossRef type=%s", paper["filename"], cr_type)
                    continue

                paper["cr_type"] = cr_type
                container = cr_match.get("container-title", [])
                paper["s2_venue"] = container[0] if container else None
                paper["s2_pub_types"] = [cr_type] if cr_type else []
                cr_doi = cr_match.get("DOI")
                if cr_doi:
                    paper["s2_external_ids"] = {"DOI": cr_doi}
                cr_url = cr_match.get("URL")
                if cr_url and not paper.get("s2_url"):
                    paper["s2_url"] = cr_url
                passed.append(paper)
            else:
                # Not found in either — keep but flag
                paper["s2_pub_types"] = []
                paper["s2_venue"] = None
                paper["_manual_review"] = True
                passed.append(paper)
                logger.info("Paper not found in S2 or CrossRef, keeping: %s", paper["filename"])

    # ---- Venue backfill passes ----
    # Resolve missing/preprint venues before writing results so downstream
    # stages always have the best venue available.
    needs_venue = [
        p for p in passed
        if not p.get("s2_venue") or "rxiv" in (p.get("s2_venue") or "").lower()
    ]
    if needs_venue:
        logger.info("Venue backfill: %d papers need venue resolution", len(needs_venue))

        # Pass 3: Preprint → journal lookup
        preprint_candidates = [
            p for p in needs_venue
            if p.get("cr_type") == "posted-content"
            or "rxiv" in (p.get("s2_venue") or "").lower()
        ]
        venue_resolved = {}
        if preprint_candidates:
            logger.info("Venue pass 3: %d preprint candidates, searching for journal-article versions...",
                        len(preprint_candidates))
            p3_matches = asyncio.run(_crossref_venue_lookup(
                preprint_candidates,
                use_year=False,
                type_filter="type:journal-article",
                label="Venue pass 3 (preprint→journal)",
            ))
            venue_resolved.update(p3_matches)
            logger.info("Venue pass 3 matched %d / %d preprints", len(p3_matches), len(preprint_candidates))

        # Pass 4: Full-title retry for papers still missing venue
        still_need = [p for p in needs_venue if p["filename"] not in venue_resolved and not p.get("s2_venue")]
        if still_need:
            logger.info("Venue pass 4: %d papers still missing venue, retrying CrossRef without year filter...",
                        len(still_need))
            p4_matches = asyncio.run(_crossref_venue_lookup(
                still_need,
                use_year=False,
                type_filter=None,
                label="Venue pass 4 (no-year retry)",
            ))
            venue_resolved.update(p4_matches)
            logger.info("Venue pass 4 matched %d / %d papers", len(p4_matches), len(still_need))

        # Pass 5: EuropePMC fallback for anything still missing
        still_need = [p for p in needs_venue if p["filename"] not in venue_resolved and not p.get("s2_venue")]
        if still_need:
            logger.info("Venue pass 5: %d papers still missing venue, trying EuropePMC...",
                        len(still_need))
            p5_matches = asyncio.run(_europepmc_venue_lookup(still_need))
            venue_resolved.update(p5_matches)
            logger.info("Venue pass 5 matched %d / %d papers", len(p5_matches), len(still_need))

        # Apply resolved venues to passed papers
        if venue_resolved:
            for paper in passed:
                match = venue_resolved.get(paper["filename"])
                if match:
                    paper["s2_venue"] = match["venue"]
                    if match.get("doi"):
                        if not paper.get("s2_external_ids"):
                            paper["s2_external_ids"] = {}
                        if not paper["s2_external_ids"].get("DOI"):
                            paper["s2_external_ids"]["DOI"] = match["doi"]
                    if match.get("url") and not paper.get("s2_url"):
                        paper["s2_url"] = match["url"]
            logger.info("Venue backfill: resolved %d venues total", len(venue_resolved))

    out_dir = results_dir(folder)
    with open(os.path.join(out_dir, "stage2_s2_results.json"), "w") as f:
        json.dump(passed, f, indent=2)
    if rejected:
        with open(os.path.join(out_dir, "stage2_rejected.json"), "w") as f:
            json.dump(rejected, f, indent=2)

    logger.info("Stage 2 complete: %d passed, %d rejected", len(passed), len(rejected))
    return passed


# ---------------------------------------------------------------------------
# Stage 3: PDF Compress + Gemini Batch
# ---------------------------------------------------------------------------

def _compress_pdf_gs(pdf_path: str, out_path: str, setting: str) -> bool:
    """Run Ghostscript compression at the given quality setting. Returns True on success."""
    import subprocess
    try:
        result = subprocess.run(
            [
                "gs", "-sDEVICE=pdfwrite", f"-dCompatibilityLevel=1.4",
                f"-dPDFSETTINGS=/{setting}",
                "-dNOPAUSE", "-dBATCH", "-dQUIET",
                f"-sOutputFile={out_path}", pdf_path,
            ],
            capture_output=True, timeout=120,
        )
        return result.returncode == 0 and os.path.exists(out_path)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Ghostscript %s failed for %s: %s", setting, os.path.basename(pdf_path), e)
        return False


def _compress_pdf(pdf_path: str, out_dir: str) -> str | None:
    """Compress a PDF if > 50MB. Returns path to use (original or compressed).

    Strategy: try Ghostscript /printer (300 DPI), then /ebook (150 DPI), then /screen (72 DPI).
    Falls back to pypdf lossless compression if gs is unavailable.
    """
    size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    if size_mb <= 50:
        return pdf_path

    compressed_dir = os.path.join(out_dir, "compressed")
    os.makedirs(compressed_dir, exist_ok=True)
    out_path = os.path.join(compressed_dir, os.path.basename(pdf_path))

    # Try Ghostscript: /printer (300 DPI) then /ebook (150 DPI) then /screen (72 DPI)
    import shutil
    if shutil.which("gs"):
        for setting in ("printer", "ebook", "screen"):
            if os.path.exists(out_path):
                os.remove(out_path)
            if _compress_pdf_gs(pdf_path, out_path, setting):
                new_size = os.path.getsize(out_path) / (1024 * 1024)
                if new_size <= 50:
                    logger.info(
                        "Ghostscript /%s compressed %s: %.1fMB -> %.1fMB",
                        setting, os.path.basename(pdf_path), size_mb, new_size,
                    )
                    return out_path
                logger.info(
                    "Ghostscript /%s: %.1fMB -> %.1fMB (still > 50MB, trying next)",
                    setting, size_mb, new_size,
                )
        # No setting got it under 50MB
        if os.path.exists(out_path):
            os.remove(out_path)
        logger.warning("Ghostscript could not compress %s below 50MB. Skipping.", os.path.basename(pdf_path))
        return None

    # Fallback: pypdf lossless compression
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(pdf_path)
        writer = PdfWriter(clone_from=reader)
        for page in writer.pages:
            page.compress_content_streams()
        writer.compress_identical_objects()

        with open(out_path, "wb") as f:
            writer.write(f)

        new_size = os.path.getsize(out_path) / (1024 * 1024)
        if new_size > 50:
            logger.warning("pypdf still > 50MB after compression (%.1fMB): %s", new_size, pdf_path)
            os.remove(out_path)
            return None

        logger.info("pypdf compressed %s: %.1fMB -> %.1fMB", os.path.basename(pdf_path), size_mb, new_size)
        return out_path

    except Exception as e:
        logger.error("Failed to compress %s: %s", pdf_path, e)
        return None


def _build_batch_request(pdf_uri: str, prompt: str, custom_id: str) -> dict:
    return {
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"file_data": {"mime_type": "application/pdf", "file_uri": pdf_uri}},
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
                "max_output_tokens": 65536,
            },
        },
        "custom_id": custom_id,
    }


def _load_existing_results(folder: str) -> dict:
    """Load already-parsed Gemini results from stage3_gemini_parsed.json. Returns {filename: result}."""
    parsed_path = os.path.join(results_dir(folder), "stage3_gemini_parsed.json")
    if not os.path.exists(parsed_path):
        return {}
    try:
        with open(parsed_path, "r") as f:
            existing = json.load(f)
        return {r["filename"]: r for r in existing}
    except Exception:
        return {}


def stage3_submit(folder: str, papers: list, state: dict, dry_run: bool) -> str | None:
    """Upload PDFs, build JSONL, submit batch job. Returns job_id.

    Skips papers that already have results in stage3_gemini_parsed.json to avoid
    re-processing papers that were successfully parsed in a previous run.
    Returns "no_new_papers" if all papers already have results.
    """
    out_dir = results_dir(folder)
    engine = PaperQueryEngine()
    prompt = engine.prompt_template
    api_key = os.environ.get("GOOGLE_API_KEY")

    # Check which papers already have results
    existing = _load_existing_results(folder)
    if existing:
        logger.info("Found %d papers with existing results", len(existing))

    requests_list = []
    skipped = []

    for paper in papers:
        pdf_path = paper["file_path"]
        fname = paper["filename"]

        # Skip papers that already have extracted results
        if fname in existing:
            logger.info("Skipping %s (already has results)", fname)
            continue

        if dry_run:
            size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            if size_mb > 50:
                logger.info("[DRY RUN] Would compress %s (%.1fMB > 50MB)", fname, size_mb)
            requests_list.append({"custom_id": fname, "dry_run": True})
            continue

        # Compress if needed
        usable_path = _compress_pdf(pdf_path, out_dir)
        if not usable_path:
            skipped.append({"filename": fname, "reason": "too_large_after_compress"})
            continue

        # Upload to GCS
        if fname in state.get("files", {}):
            gcs_uri = state["files"][fname]
            logger.info("Using cached GCS URI for %s", fname)
        else:
            if not GCS_BUCKET_NAME:
                logger.error("GCS_BUCKET_NAME not set. Cannot upload.")
                return None
            blob_name = f"batch_paper_pipeline/{fname}"
            try:
                gcs_uri = upload_to_gcs(usable_path, blob_name, GCS_BUCKET_NAME)
                state.setdefault("files", {})[fname] = gcs_uri
                save_state(folder, state)
            except Exception as e:
                logger.error("Failed to upload %s: %s", fname, e)
                skipped.append({"filename": fname, "reason": f"upload_error: {e}"})
                continue

        req = _build_batch_request(gcs_uri, prompt, fname)
        requests_list.append(req)

    if skipped:
        logger.warning("Skipped %d files during upload", len(skipped))

    if not requests_list:
        if existing:
            logger.info("All papers already have results. Nothing new to submit.")
            return "no_new_papers"
        logger.error("No valid requests generated.")
        return None

    if dry_run:
        logger.info("[DRY RUN] Would submit %d new requests (skipped %d with existing results)",
                     len(requests_list), len(existing))
        return "dry_run_job"

    logger.info("Submitting %d new papers (skipped %d with existing results)",
                 len(requests_list), len(existing))

    # Write JSONL
    jsonl_path = os.path.join(out_dir, "batch_input.jsonl")
    with open(jsonl_path, "w") as f:
        for r in requests_list:
            f.write(json.dumps(r) + "\n")

    # Submit
    model_name = os.environ.get("GEMINI_MODEL_NAME", "gemini-3.1-pro-preview")
    job_id = submit_batch_job(
        jsonl_path=jsonl_path,
        model_name=model_name,
        display_name=f"paper_pipeline_{int(time.time())}",
        api_key=api_key,
    )
    logger.info("Batch submitted: %s", job_id)
    return job_id


def stage3_poll(job_id: str, poll_interval: int, dry_run: bool) -> bool:
    """Poll until job completes. Returns True on success."""
    if dry_run:
        logger.info("[DRY RUN] Skipping poll")
        return True

    api_key = os.environ.get("GOOGLE_API_KEY")
    logger.info("Polling job %s every %ds...", job_id, poll_interval)

    while True:
        status = check_job_status(job_id, api_key)
        if status and ("SUCCEEDED" in status or "COMPLETED" in status):
            logger.info("Job succeeded!")
            return True
        elif status and any(x in status for x in ["FAILED", "CANCELLED", "EXPIRED"]):
            logger.error("Job failed with status: %s", status)
            return False
        time.sleep(poll_interval)


def stage3_download(folder: str, job_id: str, dry_run: bool) -> str | None:
    """Download batch results. Returns path to raw JSONL."""
    if dry_run:
        logger.info("[DRY RUN] Skipping download")
        return None

    out_dir = results_dir(folder)
    api_key = os.environ.get("GOOGLE_API_KEY")
    path = download_batch_results(job_id, out_dir, api_key)
    if not path or not os.path.exists(path):
        logger.error("Failed to download results")
        return None

    # Copy to deterministic name
    raw_path = os.path.join(out_dir, "stage3_raw_results.jsonl")
    if path != raw_path:
        import shutil
        shutil.copy2(path, raw_path)

    return raw_path


def stage3_parse(folder: str, papers: list, raw_results_path: str) -> list:
    """Parse raw Gemini JSONL into structured drug records (no verification).

    Merges newly parsed results with any existing results from a previous run,
    so incremental batch submissions produce a complete combined output.
    Saves to stage3_gemini_parsed.json.
    """
    # Build filename -> paper metadata map
    paper_map = {p["filename"]: p for p in papers}

    # Start with existing parsed results from previous runs
    existing = _load_existing_results(folder)
    # Update cached results' journal from current stage2 data (venue backfill
    # may have resolved venues after the cache was written)
    for cached in existing.values():
        fn = cached.get("filename")
        s2_meta = paper_map.get(fn, {})
        s2_venue = s2_meta.get("s2_venue")
        cur_journal = cached.get("journal") or ""
        if s2_venue and (not cur_journal or "rxiv" in cur_journal.lower()):
            cached["journal"] = s2_venue
    all_parsed = list(existing.values())
    existing_filenames = set(existing.keys())

    with open(raw_results_path, "r") as f:
        for line in f:
            try:
                item = json.loads(line)
            except json.JSONDecodeError as e:
                logger.error("JSONL parse error: %s", e)
                continue

            filename = item.get("custom_id", "")

            # Skip papers we already have parsed results for
            if filename in existing_filenames:
                continue

            if "error" in item:
                logger.error("API error for %s: %s", filename, item["error"])
                continue

            candidates = item.get("response", {}).get("candidates", [])
            if not candidates:
                logger.warning("No candidates for %s", filename)
                continue

            parts = candidates[0].get("content", {}).get("parts") or []
            text_part = "".join(
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("text") and not p.get("thought")
            )
            parsed_json = extract_json_from_llm_response(text_part)
            if not parsed_json:
                logger.error("Failed to parse JSON for %s", filename)
                continue

            extracted_drugs = parsed_json.get("drugs", [])
            additional_drugs = parsed_json.get("additional_drugs", [])
            extracted_title = parsed_json.get("title", filename)

            # Attach S2 metadata from stage2
            s2_meta = paper_map.get(filename, {})

            parsed_result = {
                "filename": filename,
                "title": extracted_title,
                "author": s2_meta.get("author", ""),
                "year": s2_meta.get("year"),
                "journal": s2_meta.get("s2_venue"),
                "s2_pub_types": s2_meta.get("s2_pub_types", []),
                "extracted_drugs": extracted_drugs,
                "additional_drugs": additional_drugs,
                "truncated": parsed_json.get("truncated", False),
            }
            all_parsed.append(parsed_result)

    out_dir = results_dir(folder)
    with open(os.path.join(out_dir, "stage3_gemini_parsed.json"), "w") as f:
        json.dump(all_parsed, f, indent=2)

    new_count = len(all_parsed) - len(existing_filenames)
    logger.info(
        "Stage 3 parse: %d total papers (%d new, %d from previous run)",
        len(all_parsed), new_count, len(existing_filenames),
    )
    return all_parsed


def stage3_verify(folder: str, max_workers: int = 20) -> list:
    """Verify parsed drug records against the DB. Reads stage3_gemini_parsed.json,
    writes stage3_parsed.json in the format consumed by stage 4/5.

    LLM target-match calls are parallelized with up to `max_workers` threads.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out_dir = results_dir(folder)
    parsed_path = os.path.join(out_dir, "stage3_gemini_parsed.json")
    with open(parsed_path, "r") as f:
        gemini_parsed = json.load(f)

    engine = PaperQueryEngine()
    index = DrugIndex.from_db()   # every drug by name and synonym, built once for the run

    # --- Pass 1: filter drugs and collect work items ---
    # Each work item is (paper_idx, drug_order, drug_data).
    # additional_drugs_by_paper collects non-targeted drugs per paper.
    work_items = []
    additional_drugs_by_paper = {}

    for paper_idx, paper in enumerate(gemini_parsed):
        extracted_drugs = list(paper.get("extracted_drugs", []))
        additional_drugs = list(paper.get("additional_drugs", []))
        drug_order = 0

        for drug in extracted_drugs:
            norm_drug = normalize_key(drug.get("name", ""))
            if norm_drug in BATCH_EXCLUDED_DRUGS:
                continue
            if norm_drug in ALWAYS_NON_TARGETED:
                additional_drugs.append(drug)
                continue
            _t = (drug.get("target") or "").strip()
            _g = (drug.get("human_target_gene") or "").strip()
            no_target = _t.lower() in ("", "none") and _g.lower() in ("", "none")
            no_claim = False
            if "target_claim_sentence" in drug:
                _c = (drug["target_claim_sentence"] or "").strip()
                no_claim = _c.lower() in ("", "none")
            if no_target or no_claim:
                additional_drugs.append(drug)
                continue
            work_items.append((paper_idx, drug_order, drug))
            drug_order += 1

        additional_drugs_by_paper[paper_idx] = additional_drugs

    logger.info("Stage 3 verify: %d papers, %d drugs to verify (%d workers)",
                len(gemini_parsed), len(work_items), max_workers)

    # --- Pass 2: parallel LLM verification ---
    # Results keyed by (paper_idx, drug_order) to preserve ordering.
    verified_by_paper = {}
    completed = 0

    def _verify_one(item):
        paper_idx, drug_order, drug = item
        return paper_idx, drug_order, engine._verify_drug(drug, index)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_verify_one, item): item for item in work_items}
        for future in as_completed(futures):
            paper_idx, drug_order, result = future.result()
            verified_by_paper.setdefault(paper_idx, []).append((drug_order, result))
            completed += 1
            if completed % 500 == 0:
                logger.info("Stage 3 verify progress: %d/%d drugs", completed, len(work_items))

    # --- Pass 3: reassemble results per paper (in original order) ---
    all_paper_results = []
    for paper_idx, paper in enumerate(gemini_parsed):
        items = verified_by_paper.get(paper_idx, [])
        items.sort(key=lambda x: x[0])
        verified_results = [r for _, r in items]

        # Enrich with alternates for yellow/red
        for v_item in verified_results:
            if v_item.get("verdict_icon") in ["🟡", "🔴"]:
                alternates = []
                seen = set()
                extracted_gene = v_item.get("human_target_gene", "")
                potential_genes = set()
                if extracted_gene and extracted_gene != "None":
                    for t in re.split(r"[,\/]+", extracted_gene):
                        t = t.strip()
                        if t:
                            potential_genes.add(t)

                for gene in potential_genes:
                    for alt in get_verified_selective_drugs_by_target(gene):
                        if alt != v_item.get("name") and alt not in seen:
                            seen.add(alt)
                            alternates.append(alt)

                v_item["alternate_compounds"] = alternates
            else:
                v_item["alternate_compounds"] = []

        paper_result = {
            "filename": paper["filename"],
            "title": paper.get("title", ""),
            "author": paper.get("author", ""),
            "year": paper.get("year"),
            "journal": paper.get("journal"),
            "s2_pub_types": paper.get("s2_pub_types", []),
            "results": verified_results,
            "additional_drugs": additional_drugs_by_paper.get(paper_idx, []),
            "truncated": paper.get("truncated", False),
        }
        all_paper_results.append(paper_result)

    with open(os.path.join(out_dir, "stage3_parsed.json"), "w") as f:
        json.dump(all_paper_results, f, indent=2)

    total_drugs = sum(len(p["results"]) for p in all_paper_results)
    logger.info(
        "Stage 3 verify: %d papers, %d verified drugs",
        len(all_paper_results), total_drugs,
    )
    return all_paper_results


# ---------------------------------------------------------------------------
# Stage 4: Post-Gemini Filtering
# ---------------------------------------------------------------------------

def stage4_filter(folder: str, paper_results: list, min_targeted_drugs: int) -> list:
    """Filter out papers with too few targeted drugs."""
    passed = []
    filter_log = []

    for paper in paper_results:
        # Count all results as targeted (they passed ALWAYS_NON_TARGETED filter already)
        n_targeted = len(paper.get("results", []))

        reasons = []

        if n_targeted < min_targeted_drugs:
            reasons.append(f"only_{n_targeted}_targeted_drugs")

        if reasons:
            filter_log.append({
                "filename": paper["filename"],
                "title": paper["title"],
                "reasons": reasons,
                "targeted_count": n_targeted,
            })
        else:
            passed.append(paper)

    out_dir = results_dir(folder)
    with open(os.path.join(out_dir, "stage4_filtered.json"), "w") as f:
        json.dump(passed, f, indent=2)
    with open(os.path.join(out_dir, "stage4_filter_log.json"), "w") as f:
        json.dump(filter_log, f, indent=2)

    logger.info("Stage 4: %d passed, %d filtered out", len(passed), len(filter_log))
    return passed


# ---------------------------------------------------------------------------
# Stage 5: Report Generation
# ---------------------------------------------------------------------------

def _classify_drug(item: dict) -> str:
    """Classify a verified drug item into a category."""
    icon = item.get("verdict_icon", "⚪")
    selectivity = (item.get("selectivity") or "Unknown").upper()

    if icon == "🟢":
        if selectivity == "SELECTIVE":
            return "verified_selective"
        elif selectivity == "MODERATELY SELECTIVE":
            return "verified_mod_selective"
        else:
            return "verified_selective"  # green is always good selectivity
    elif icon == "🟡":
        if selectivity == "PROMISCUOUS":
            return "verified_promiscuous"
        else:
            return "verified_unknown"
    elif icon == "🔴":
        return "unverified"
    else:  # ⚪
        return "not_in_db"


def _write_csv(path: str, rows: list, columns: list):
    """Write a list of dicts to CSV."""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _build_pipeline_funnel(folder: str, paper_results: list) -> list:
    """Build pipeline funnel stats by reading stage JSON files.

    Returns list of (stage_label, count) tuples.
    """
    out_dir = results_dir(folder)
    funnel = []

    # Total PDFs in folder
    n_pdfs = len(glob.glob(os.path.join(folder, "*.pdf")))
    funnel.append(("PDFs in directory", n_pdfs))

    # Stage 1: parsed
    s1_path = os.path.join(out_dir, "stage1_parsed.json")
    if os.path.exists(s1_path):
        with open(s1_path, "r") as f:
            s1 = json.load(f)
        funnel.append(("Passed filename/year filter", len(s1)))

    # Stage 2: S2 checked
    s2_path = os.path.join(out_dir, "stage2_s2_results.json")
    if os.path.exists(s2_path):
        with open(s2_path, "r") as f:
            s2 = json.load(f)
        funnel.append(("Passed type filter (S2/CrossRef)", len(s2)))

    # Stage 3: verified against DB (uses stage3_parsed which is the final
    # merged result of Gemini extraction + DB verification)
    s3v_path = os.path.join(out_dir, "stage3_parsed.json")
    if os.path.exists(s3v_path):
        with open(s3v_path, "r") as f:
            s3v = json.load(f)
        funnel.append(("Extracted & verified", len(s3v)))

    # Stage 4: filtered
    funnel.append(("Passed drug-count filter", len(paper_results)))

    return funnel


def stage5_reports(folder: str, paper_results: list):
    """Generate CSV reports and HTML dashboard."""
    out_dir = results_dir(folder)
    report_dir = os.path.join(out_dir, "stage5_reports")
    os.makedirs(report_dir, exist_ok=True)

    # ---- Pipeline funnel ----
    funnel = _build_pipeline_funnel(folder, paper_results)

    # ---- Sync venues and URLs from stage2 (authoritative source) ----
    # Stage2 has the most complete venue data (including backfill passes).
    # Stage3/4 cached results may have stale or missing journal values,
    # so we patch paper_results before collecting any data.
    s2_url_map = {}
    s2_results_path = os.path.join(out_dir, "stage2_s2_results.json")
    if os.path.exists(s2_results_path):
        with open(s2_results_path, "r") as f:
            s2_papers = json.load(f)
        s2_venue_map = {}
        for sp in s2_papers:
            fname = sp.get("filename", "")
            ext_ids = sp.get("s2_external_ids") or {}
            doi = ext_ids.get("DOI")
            if doi:
                s2_url_map[fname] = f"https://doi.org/{doi}"
            elif sp.get("s2_url"):
                s2_url_map[fname] = sp["s2_url"]
            s2_venue = sp.get("s2_venue")
            if s2_venue:
                s2_venue_map[fname] = s2_venue

        venue_patched = 0
        for paper in paper_results:
            fn = paper.get("filename", "")
            cur = paper.get("journal") or ""
            new = s2_venue_map.get(fn)
            if new and (not cur or "rxiv" in cur.lower()):
                paper["journal"] = new
                venue_patched += 1
        if venue_patched:
            logger.info("Synced %d journals from stage2 venues", venue_patched)

    # ---- Live venue backfill for papers still missing journal ----
    # Uses strategies unavailable at stage2 time: stage3 full titles, DOI
    # prefix mapping, and PubMed search.
    still_missing = [p for p in paper_results if not (p.get("journal") or "").strip()]
    if still_missing:
        all_resolved = {}  # filename -> venue string
        s2_by_fn = {sp["filename"]: sp for sp in s2_papers} if os.path.exists(s2_results_path) else {}

        # Pass A: DOI prefix → journal name (for AACR supplementary DOIs etc.)
        for p in still_missing:
            fn = p["filename"]
            doi = ((s2_by_fn.get(fn) or {}).get("s2_external_ids") or {}).get("DOI", "")
            for prefix, journal in DOI_JOURNAL_MAP.items():
                if doi.startswith(prefix):
                    all_resolved[fn] = journal
                    break
        if all_resolved:
            logger.info("Report venue backfill: %d resolved via DOI prefix mapping", len(all_resolved))

        # Pass B: CrossRef with full stage3 titles, no year filter
        still_need = [p for p in still_missing if p["filename"] not in all_resolved]
        if still_need:
            enhanced = [{"filename": p["filename"], "title": p.get("title", ""), "year": p.get("year")}
                        for p in still_need]
            logger.info("Report venue backfill: %d remaining, trying CrossRef with full titles...", len(enhanced))
            try:
                cr_matches = asyncio.run(_crossref_venue_lookup(
                    enhanced, use_year=False, type_filter=None,
                    label="Report venue backfill (CrossRef)",
                ))
                for fn, m in cr_matches.items():
                    all_resolved[fn] = m["venue"]
                if cr_matches:
                    logger.info("Report venue backfill: %d resolved via CrossRef", len(cr_matches))
            except Exception as e:
                logger.warning("Report venue backfill CrossRef failed: %s", e)

        # Pass C: EuropePMC with full stage3 titles
        still_need = [p for p in still_missing if p["filename"] not in all_resolved]
        if still_need:
            logger.info("Report venue backfill: %d remaining, trying EuropePMC...", len(still_need))
            try:
                epmc_matches = asyncio.run(_europepmc_venue_lookup(still_need))
                for fn, m in epmc_matches.items():
                    all_resolved[fn] = m["venue"]
                if epmc_matches:
                    logger.info("Report venue backfill: %d resolved via EuropePMC", len(epmc_matches))
            except Exception as e:
                logger.warning("Report venue backfill EuropePMC failed: %s", e)

        # Pass D: PubMed title search
        still_need = [p for p in still_missing if p["filename"] not in all_resolved]
        if still_need:
            logger.info("Report venue backfill: %d remaining, trying PubMed...", len(still_need))
            try:
                pm_matches = asyncio.run(_pubmed_venue_lookup(still_need))
                all_resolved.update(pm_matches)
                if pm_matches:
                    logger.info("Report venue backfill: %d resolved via PubMed", len(pm_matches))
            except Exception as e:
                logger.warning("Report venue backfill PubMed failed: %s", e)

        # Apply all resolved venues to paper_results
        if all_resolved:
            for paper in paper_results:
                venue = all_resolved.get(paper["filename"])
                if venue:
                    paper["journal"] = venue

            # Persist back to stage2 for future runs
            if s2_by_fn:
                for fn, venue in all_resolved.items():
                    sp = s2_by_fn.get(fn)
                    if sp and not sp.get("s2_venue"):
                        sp["s2_venue"] = venue
                with open(s2_results_path, "w") as f:
                    json.dump(s2_papers, f, indent=2)

            final_missing = sum(1 for p in paper_results if not (p.get("journal") or "").strip())
            logger.info("Report venue backfill: resolved %d total, %d still missing",
                        len(all_resolved), final_missing)

    # ---- Collect data ----
    manuscripts = []
    drug_records = []  # flat list of all drug mentions with metadata
    # Frequency counters count unique *papers* per drug/gene, not raw mentions.
    drug_paper_sets = defaultdict(set)       # freq_key -> set of paper filenames
    gene_paper_verified = defaultdict(set)   # gene -> set of paper filenames
    gene_paper_unverified = defaultdict(set)
    not_in_db_paper_sets = defaultdict(set)
    gene_drugs = defaultdict(dict)  # gene -> {freq_key: {drug, verdict, sel, icon, papers}}
    # Per-gene drug counts by verdict category (unique drugs, not mentions)
    gene_drug_sets = defaultdict(lambda: {"verified": set(), "unverified": set(), "not_in_db": set()})

    # Category buckets
    categories = defaultdict(list)

    for paper in paper_results:
        paper_title = paper.get("title", "")
        paper_filename = paper.get("filename", "")
        drug_items = paper.get("results", [])
        not_in_db_count = sum(1 for d in drug_items if _classify_drug(d) == "not_in_db")
        manuscripts.append({
            "Title": paper_title,
            "Journal": paper.get("journal", ""),
            "Year": paper.get("year", ""),
            "Author": paper.get("author", ""),
            "Filename": paper_filename,
            "drugCount": len(drug_items),
            "notInDbCount": not_in_db_count,
        })

        for drug_item in drug_items:
            cat = _classify_drug(drug_item)
            drug_name = drug_item.get("name", "")
            # Use DB-matched name for frequency counting so paper variants
            # (e.g. "JQ1", "(+)-JQ1") roll up under the canonical DB name.
            freq_key = drug_item.get("db_match") or drug_name
            drug_paper_sets[freq_key].add(paper_filename)

            # DB target genes come from the database lookup, not Gemini extraction
            db_tg = drug_item.get("db_target_genes", [])
            if isinstance(db_tg, str):
                db_tg = [g.strip() for g in re.split(r"[,;/]+", db_tg) if g.strip()]
            db_gene_str = ", ".join(db_tg) if db_tg else ""

            # Collect gene frequencies from paper-claimed target only —
            # frequencies reflect what papers intend/claim, not DB targets.
            paper_gene_str = drug_item.get("human_target_gene", "")
            if paper_gene_str and paper_gene_str != "None":
                gene_list = [g.strip().upper() for g in re.split(r"[,;/]+", paper_gene_str)
                             if g.strip()]
            else:
                gene_list = []
            icon = drug_item.get("verdict_icon", "")
            icon_label = {"🟢": "green", "🟡": "yellow", "🔴": "red", "⚪": "gray"}.get(icon, "gray")
            if cat == "not_in_db":
                # Track not-in-db drug counts per gene, but exclude from
                # paper frequencies and gene_drugs detail.
                for g in gene_list:
                    gene_drug_sets[g]["not_in_db"].add(freq_key)
                gene_list = []
            for g in gene_list:
                if cat == "unverified":
                    gene_paper_unverified[g].add(paper_filename)
                    gene_drug_sets[g]["unverified"].add(freq_key)
                else:
                    gene_paper_verified[g].add(paper_filename)
                    gene_drug_sets[g]["verified"].add(freq_key)
                # Track drug detail for this gene
                if freq_key not in gene_drugs[g]:
                    gene_drugs[g][freq_key] = {
                        "d": freq_key,
                        "v": drug_item.get("verdict_text", ""),
                        "s": drug_item.get("selectivity", ""),
                        "i": icon_label,
                        "papers": [],
                    }
                gene_drugs[g][freq_key]["papers"].append({
                    "t": paper_title,
                    "f": paper_filename,
                })

            if cat == "not_in_db":
                not_in_db_paper_sets[freq_key].add(paper_filename)

            has_alts = bool(drug_item.get("alternate_compounds"))

            # Selectivity only meaningful for drugs found in DB
            sel_str = drug_item.get("selectivity", "")
            if cat == "not_in_db":
                sel_str = ""

            gene_str = drug_item.get("human_target_gene", "")
            record = {
                "Drug": drug_name,
                "Paper Target": drug_item.get("paper_target", ""),
                "Human Target Gene": gene_str,
                "DB Target Genes": db_gene_str,
                "Verdict": drug_item.get("verdict_text", ""),
                "Selectivity": sel_str,
                "Category": cat,
                "DB Match": drug_item.get("db_match", ""),
                "Alternate Compounds": ", ".join(drug_item.get("alternate_compounds", [])),
                "Paper": paper.get("filename", ""),
                "Paper Title": paper.get("title", ""),
            }
            drug_records.append(record)
            categories[cat].append(record)

            # Also track alts-specific categories
            if has_alts and cat == "verified_promiscuous":
                categories["verified_promiscuous_alts"].append(record)
            elif has_alts and cat == "verified_unknown":
                categories["verified_unknown_alts"].append(record)
            elif has_alts and cat == "unverified":
                categories["unverified_alts"].append(record)
            elif has_alts and cat == "not_in_db":
                categories["not_in_db_alts"].append(record)

    # ---- Build paper-level frequency Counters from sets ----
    drug_freq = Counter({k: len(v) for k, v in drug_paper_sets.items()})
    not_in_db_drugs = Counter({k: len(v) for k, v in not_in_db_paper_sets.items()})
    gene_freq_verified = Counter({g: len(v) for g, v in gene_paper_verified.items()})
    gene_freq_unverified = Counter({g: len(v) for g, v in gene_paper_unverified.items()})

    # ---- Write CSVs ----

    # manuscripts.csv
    _write_csv(
        os.path.join(report_dir, "manuscripts.csv"),
        manuscripts,
        ["Title", "Journal", "Year", "Author", "Filename"],
    )

    # drug_freq_all_targeted.csv
    _write_csv(
        os.path.join(report_dir, "drug_freq_all_targeted.csv"),
        [{"Drug": d, "Count": c} for d, c in drug_freq.most_common()],
        ["Drug", "Count"],
    )

    # drug_freq_not_in_db.csv
    _write_csv(
        os.path.join(report_dir, "drug_freq_not_in_db.csv"),
        [{"Drug": d, "Count": c} for d, c in not_in_db_drugs.most_common()],
        ["Drug", "Count"],
    )

    # gene_frequencies.csv — single table with verified/unverified breakdown + drug counts
    all_genes = sorted(set(gene_freq_verified) | set(gene_freq_unverified) | set(gene_drug_sets),
                       key=lambda g: gene_freq_verified[g] + gene_freq_unverified[g],
                       reverse=True)
    _write_csv(
        os.path.join(report_dir, "gene_frequencies.csv"),
        [{"Gene": g,
          "Total Papers": gene_freq_verified[g] + gene_freq_unverified[g],
          "Verified Papers": gene_freq_verified[g],
          "Unverified Papers": gene_freq_unverified[g],
          "Total Drugs": len(gene_drug_sets[g]["verified"]) + len(gene_drug_sets[g]["unverified"]),
          "Verified Drugs": len(gene_drug_sets[g]["verified"]),
          "Unverified Drugs": len(gene_drug_sets[g]["unverified"]),
          "Not-in-DB Drugs": len(gene_drug_sets[g]["not_in_db"])}
         for g in all_genes],
        ["Gene", "Total Papers", "Verified Papers", "Unverified Papers",
         "Total Drugs", "Verified Drugs", "Unverified Drugs", "Not-in-DB Drugs"],
    )

    # Per-category CSVs
    cat_columns = ["Drug", "Paper Target", "Human Target Gene", "DB Target Genes", "Verdict", "Selectivity",
                    "DB Match", "Alternate Compounds", "Paper", "Paper Title"]

    category_files = {
        "verified_selective": "verified_selective.csv",
        "verified_mod_selective": "verified_mod_selective.csv",
        "verified_promiscuous": "verified_promiscuous.csv",
        "verified_promiscuous_alts": "verified_promiscuous_alts.csv",
        "verified_unknown": "verified_unknown.csv",
        "verified_unknown_alts": "verified_unknown_alts.csv",
        "unverified": "unverified.csv",
        "unverified_alts": "unverified_alts.csv",
        "not_in_db": "not_in_db.csv",
        "not_in_db_alts": "not_in_db_alts.csv",
    }

    for cat_key, csv_name in category_files.items():
        rows = categories.get(cat_key, [])
        _write_csv(os.path.join(report_dir, csv_name), rows, cat_columns)

    # ---- Pipeline summary CSV ----
    _write_csv(
        os.path.join(report_dir, "pipeline_summary.csv"),
        [{"Stage": label, "Count": count} for label, count in funnel],
        ["Stage", "Count"],
    )

    # ---- Paper-level stats + audit CSV ----
    # "good" = verified target match AND (selective or moderately selective) = green 🟢
    # "not good" = yellow 🟡 or red 🔴 (excludes not-in-DB gray ⚪)
    GOOD_CATS = {"verified_selective", "verified_mod_selective"}
    papers_with_unverified = 0
    papers_with_actionable_alt = 0
    paper_audit_rows = []
    for paper in paper_results:
        has_unverified = False
        has_alt_for_not_good = False
        drug_items = paper.get("results", [])
        good_drugs = []
        not_good_drugs = []      # yellow or red (in DB)
        not_in_db_drugs_list = []
        for drug_item in drug_items:
            cat = _classify_drug(drug_item)
            name = drug_item.get("name", "")
            db_match = drug_item.get("db_match", "")
            freq_key = db_match or name
            verdict = drug_item.get("verdict_text", "")
            sel = drug_item.get("selectivity", "")
            paper_target = drug_item.get("paper_target", "")
            gene = drug_item.get("human_target_gene", "")
            alts = drug_item.get("alternate_compounds", [])
            if cat == "not_in_db":
                not_in_db_drugs_list.append(name)
                continue
            if cat in GOOD_CATS:
                good_drugs.append(freq_key)
            else:
                not_good_drugs.append({
                    "drug": freq_key,
                    "cat": cat,
                    "paper_target": paper_target,
                    "gene": gene,
                    "sel": sel,
                    "alts": alts,
                })
                if cat == "unverified":
                    has_unverified = True
                if alts:
                    has_alt_for_not_good = True
        if has_unverified:
            papers_with_unverified += 1
        if has_alt_for_not_good:
            papers_with_actionable_alt += 1

        # Build audit row — one row per paper
        unverified_names = [d["drug"] for d in not_good_drugs if d["cat"] == "unverified"]
        yellow_names = [d["drug"] for d in not_good_drugs if d["cat"] != "unverified"]
        drugs_with_alts = [
            f"{d['drug']} -> {', '.join(d['alts'])}"
            for d in not_good_drugs if d["alts"]
        ]
        paper_audit_rows.append({
            "Filename": paper.get("filename", ""),
            "Title": paper.get("title", ""),
            "Author": paper.get("author", ""),
            "Year": paper.get("year", ""),
            "Total Drugs": len(drug_items),
            "Good (green)": len(good_drugs),
            "Not Good (yellow)": len(yellow_names),
            "Unverified (red)": len(unverified_names),
            "Not in DB": len(not_in_db_drugs_list),
            "Has Unverified": "Y" if has_unverified else "",
            "Has Actionable Alt": "Y" if has_alt_for_not_good else "",
            "Unverified Drugs": "; ".join(unverified_names),
            "Yellow Drugs": "; ".join(yellow_names),
            "Recommended Alternatives": "; ".join(drugs_with_alts),
            "Good Drugs": "; ".join(good_drugs),
        })

    # Write paper-level audit CSV
    _write_csv(
        os.path.join(report_dir, "paper_verdict_audit.csv"),
        paper_audit_rows,
        ["Filename", "Title", "Author", "Year",
         "Total Drugs", "Good (green)", "Not Good (yellow)",
         "Unverified (red)", "Not in DB",
         "Has Unverified", "Has Actionable Alt",
         "Unverified Drugs", "Yellow Drugs",
         "Recommended Alternatives", "Good Drugs"],
    )
    logger.info("Wrote paper_verdict_audit.csv (%d rows)", len(paper_audit_rows))

    # Count unique drugs in DB vs not
    drugs_in_db = set()
    drugs_not_in_db = set()
    for paper in paper_results:
        for drug_item in paper.get("results", []):
            freq_key = drug_item.get("db_match") or drug_item.get("name", "")
            if _classify_drug(drug_item) == "not_in_db":
                drugs_not_in_db.add(freq_key)
            else:
                drugs_in_db.add(freq_key)

    paper_stats = {
        'total_papers': len(paper_results),
        'papers_with_unverified': papers_with_unverified,
        'papers_with_actionable_alt': papers_with_actionable_alt,
        'unique_drugs_in_db': len(drugs_in_db),
        'unique_drugs_not_in_db': len(drugs_not_in_db - drugs_in_db),
    }

    drug_papers = defaultdict(list)
    paper_details = {}  # filename -> {url, drugs: [{name, verdict, selectivity, target}]}
    for paper in paper_results:
        paper_title = paper.get("title", "")
        paper_filename = paper.get("filename", "")
        paper_url = s2_url_map.get(paper_filename, "")
        paper_drugs = []
        seen_drugs_in_paper = set()
        for drug_item in paper.get("results", []):
            drug_name = drug_item.get("name", "")
            freq_key = drug_item.get("db_match") or drug_name
            cat = _classify_drug(drug_item)
            # Map emoji icons to CSS-safe labels for the HTML report
            icon = drug_item.get("verdict_icon", "")
            icon_label = {"🟢": "green", "🟡": "yellow", "🔴": "red", "⚪": "gray"}.get(icon, "gray")
            paper_drugs.append({
                "n": freq_key,
                "v": drug_item.get("verdict_text", ""),
                "s": drug_item.get("selectivity", ""),
                "pt": drug_item.get("paper_target", ""),
                "i": icon_label,
            })
            # Deduplicate within same paper for drug_papers
            if freq_key not in seen_drugs_in_paper:
                seen_drugs_in_paper.add(freq_key)
                drug_papers[freq_key].append({
                    "t": paper_title,
                    "f": paper_filename,
                    "u": paper_url,
                    "c": cat,
                })
        paper_details[paper_filename] = {"u": paper_url, "d": paper_drugs}

    # Enrich gene_drugs paper entries with URLs from s2_url_map
    gene_drugs_serial = {}
    for gene, drug_map in gene_drugs.items():
        entries = []
        for entry in drug_map.values():
            # Add URL to each paper and deduplicate by filename
            seen_files = set()
            deduped_papers = []
            for p in entry["papers"]:
                if p["f"] not in seen_files:
                    seen_files.add(p["f"])
                    p["u"] = s2_url_map.get(p["f"], "")
                    deduped_papers.append(p)
            entry["papers"] = deduped_papers
            entries.append(entry)
        gene_drugs_serial[gene] = entries

    # Pre-compute gene_frequencies for HTML report (same data as CSV)
    gene_frequencies_data = [
        [g, gene_freq_verified[g] + gene_freq_unverified[g],
         gene_freq_verified[g], gene_freq_unverified[g],
         len(gene_drug_sets[g]["verified"]) + len(gene_drug_sets[g]["unverified"]),
         len(gene_drug_sets[g]["verified"]),
         len(gene_drug_sets[g]["unverified"]),
         len(gene_drug_sets[g]["not_in_db"])]
        for g in all_genes
    ][:100]

    # ---- HTML Dashboard ----
    _generate_html_report(report_dir, manuscripts, drug_records, drug_freq,
                          not_in_db_drugs,
                          categories, paper_results, funnel,
                          paper_stats, drug_papers, paper_details,
                          gene_drugs_serial, gene_frequencies_data)

    logger.info("Stage 5: Reports written to %s", report_dir)


def _generate_html_report(report_dir, manuscripts, drug_records, drug_freq,
                          not_in_db_drugs,
                          categories, paper_results, funnel,
                          paper_stats, drug_papers, paper_details,
                          gene_drugs, gene_frequencies_data):
    """Generate self-contained HTML dashboard."""

    # Prepare summary stats
    total_papers = len(manuscripts)
    total_drug_paper_pairs = sum(drug_freq.values())
    unique_drugs = len(drug_freq)
    cat_counts = {
        k: len({(r["DB Match"] or r["Drug"], r["Paper"]) for r in v})
        for k, v in categories.items()
    }

    # Filter log
    filter_log_path = os.path.join(os.path.dirname(report_dir), "stage4_filter_log.json")
    filter_log = []
    if os.path.exists(filter_log_path):
        with open(filter_log_path, "r") as f:
            filter_log = json.load(f)

    stage2_rejected_path = os.path.join(os.path.dirname(report_dir), "stage2_rejected.json")
    s2_rejected = []
    if os.path.exists(stage2_rejected_path):
        with open(stage2_rejected_path, "r") as f:
            s2_rejected = json.load(f)

    # Embed data as JSON
    report_data = {
        "summary": {
            "total_papers": total_papers,
            "total_drug_paper_pairs": total_drug_paper_pairs,
            "unique_drugs": unique_drugs,
            "category_counts": cat_counts,
        },
        "paper_stats": paper_stats,
        "funnel": funnel,
        "manuscripts": manuscripts,
        "gene_frequencies": gene_frequencies_data,
        "all_drugs": [r for cat in ("verified_selective", "verified_mod_selective",
                                     "verified_promiscuous", "verified_unknown",
                                     "unverified", "not_in_db")
                       for r in categories.get(cat, [])],
        "drug_papers": dict(drug_papers),
        "paper_details": paper_details,
        "gene_drugs": {g: (list(drugs.values()) if isinstance(drugs, dict) else drugs)
                       for g, drugs in gene_drugs.items()},
        "filter_log": filter_log,
        "s2_rejected": s2_rejected,
    }

    data_json = json.dumps(report_data, indent=None, default=str).replace("</", "<\\/")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Batch Paper Pipeline Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f8f9fa; color: #1a1a2e; padding: 24px;
    max-width: 1600px; margin: 0 auto;
  }}
  h1 {{ font-size: 24px; margin-bottom: 4px; color: #16213e; }}
  .subtitle {{ color: #666; font-size: 14px; margin-bottom: 24px; }}
  .tabs {{ display: flex; gap: 2px; margin-bottom: 0; flex-wrap: wrap; }}
  .tab {{
    padding: 10px 20px; background: #e2e8f0; border: none;
    cursor: pointer; font-size: 14px; border-radius: 8px 8px 0 0;
    font-weight: 500; color: #4a5568;
  }}
  .tab:hover {{ background: #cbd5e0; }}
  .tab.active {{ background: #fff; color: #16213e; font-weight: 600; }}
  .panel {{
    display: none; background: #fff; border-radius: 0 8px 8px 8px;
    padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.1);
  }}
  .panel.active {{ display: block; }}
  .stat-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }}
  .stat-card {{
    background: #f7fafc; border-radius: 8px; padding: 16px;
    border-left: 4px solid #4299e1;
  }}
  .stat-card .label {{ font-size: 12px; color: #718096; text-transform: uppercase; }}
  .stat-card .value {{ font-size: 28px; font-weight: 700; color: #2d3748; }}
  .cat-green {{ border-left-color: #48bb78; }}
  .cat-yellow {{ border-left-color: #ecc94b; }}
  .cat-red {{ border-left-color: #f56565; }}
  .cat-gray {{ border-left-color: #a0aec0; }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 16px;
  }}
  th {{
    background: #edf2f7; padding: 8px 12px; text-align: left;
    cursor: pointer; user-select: none; position: sticky; top: 0;
    border-bottom: 2px solid #cbd5e0;
  }}
  th:hover {{ background: #e2e8f0; }}
  td {{ padding: 6px 12px; border-bottom: 1px solid #e2e8f0; }}
  tr:hover {{ background: #f7fafc; }}
  .search-box {{
    width: 100%; max-width: 400px; padding: 8px 12px;
    border: 1px solid #cbd5e0; border-radius: 6px; font-size: 14px;
    margin-bottom: 16px;
  }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 11px; font-weight: 600;
  }}
  .badge-green {{ background: #c6f6d5; color: #276749; }}
  .badge-yellow {{ background: #fefcbf; color: #975a16; }}
  .badge-red {{ background: #fed7d7; color: #9b2c2c; }}
  .badge-gray {{ background: #e2e8f0; color: #4a5568; }}
  .table-wrap {{ max-height: 600px; overflow-y: auto; }}
  .freq-row {{ cursor: pointer; }}
  .freq-row:hover {{ background: #edf2f7; }}
  .detail-row td {{ padding: 4px 12px 8px 32px !important; background: #f7fafc; font-size: 12px; }}
  .detail-row ul {{ margin: 0; padding-left: 16px; }}
  .detail-row li {{ margin: 2px 0; }}
  details {{ margin: 16px 0; }}
  details summary {{
    font-size: 16px; font-weight: 600; color: #2d3748; cursor: pointer;
    padding: 8px 0; user-select: none; list-style: none;
  }}
  details summary::-webkit-details-marker {{ display: none; }}
  details summary::before {{
    content: '\\25b6'; display: inline-block; margin-right: 8px;
    font-size: 12px; transition: transform 0.15s;
  }}
  details[open] summary::before {{ transform: rotate(90deg); }}
</style>
</head>
<body>

<h1>Batch Paper Pipeline Report</h1>
<p class="subtitle">Generated {time.strftime("%Y-%m-%d %H:%M")}</p>

<div class="tabs">
  <button class="tab active" onclick="showTab('summary', this)">Summary</button>
  <button class="tab" onclick="showTab('manuscripts', this)">Manuscripts</button>
  <button class="tab" onclick="showTab('drug-freq', this)">Drug Frequencies</button>
  <button class="tab" onclick="showTab('gene-freq', this)">Gene Frequencies</button>
  <button class="tab" onclick="showTab('by-category', this)">By Category</button>
  <button class="tab" onclick="showTab('filter-log', this)">Filter Log</button>
</div>

<div id="summary" class="panel active">

  <details open>
    <summary>Pipeline Funnel</summary>
    <p style="font-size:12px;color:#888;margin:4px 0 8px;">Papers at each stage of the pipeline. Each stage filters papers from the previous stage.</p>
    <div id="funnel-container" style="max-width:600px"></div>
  </details>

  <details open>
    <summary>Drug&ndash;Paper Pairs</summary>
    <p style="font-size:12px;color:#888;margin:4px 0 8px;">Each pair = one drug in one paper. A drug appearing in 5 papers counts as 5 pairs. Deduplicated: the same drug in the same paper counts once.</p>
    <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start;margin:12px 0;">
      <div>
        <div style="font-size:36px;font-weight:700;color:#2d3748" id="stat-pairs-total">-</div>
        <div style="font-size:13px;color:#718096">total drug&ndash;paper pairs</div>
      </div>
      <div id="pairs-bar" style="flex:1;min-width:300px;max-width:600px"></div>
    </div>
    <table id="cat-summary-table" style="max-width:600px">
      <thead><tr><th>Category</th><th>Pairs</th><th>%</th><th></th></tr></thead>
      <tbody></tbody>
    </table>
  </details>

  <details open>
    <summary>Unique Drugs</summary>
    <p style="font-size:12px;color:#888;margin:4px 0 8px;">Count of distinct drug names across all papers.</p>
    <div class="stat-grid">
      <div class="stat-card"><div class="label">Total Unique Drugs</div><div class="value" id="stat-unique">-</div></div>
      <div class="stat-card cat-green"><div class="label">In Database</div><div class="value" id="stat-drugs-indb">-</div></div>
      <div class="stat-card cat-gray"><div class="label">Not in Database</div><div class="value" id="stat-drugs-notindb">-</div></div>
    </div>
  </details>

  <details open>
    <summary>Paper Coverage</summary>
    <p style="font-size:12px;color:#888;margin:4px 0 8px;">Across {len(paper_results)} analyzed papers:</p>
    <div class="stat-grid">
      <div class="stat-card cat-red"><div class="label">Papers using &ge;1 unverified drug</div><div class="value" id="stat-papers-uv">-</div></div>
      <div class="stat-card cat-yellow"><div class="label">Papers with a recommended alternative</div><div class="value" id="stat-papers-alt">-</div></div>
    </div>
    <p style="font-size:12px;color:#888;margin-top:4px;">&ldquo;Recommended alternative&rdquo; = a drug&ndash;paper pair that is not verified-selective has a verified selective substitute in the DB. Excludes not-in-DB drugs.</p>
  </details>

</div>

<div id="manuscripts" class="panel">
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;">
    <input id="ms-search" class="search-box" placeholder="Search manuscripts..." oninput="filterManuscriptTable()" style="margin-bottom:0;max-width:260px;">
    <button id="ms-btn-notindb" onclick="toggleMsFilter('notInDb')" style="padding:6px 14px;font-size:13px;border-radius:6px;border:1px solid #cbd5e0;background:#fff;cursor:pointer;">Has Not-in-DB Drugs</button>
    <button id="ms-btn-gt5" onclick="toggleMsFilter('gt5')" style="padding:6px 14px;font-size:13px;border-radius:6px;border:1px solid #cbd5e0;background:#fff;cursor:pointer;">&gt;5 Drugs</button>
    <span id="ms-count" style="font-size:13px;color:#666;margin-left:8px;"></span>
  </div>
  <div class="table-wrap" style="max-height:none"><table id="ms-table"><thead><tr>
    <th onclick="sortTable('ms-table',0)">Title</th>
    <th onclick="sortTable('ms-table',1)">Journal</th>
    <th onclick="sortTable('ms-table',2)">Year</th>
    <th onclick="sortTable('ms-table',3)">Author</th>
    <th onclick="sortTable('ms-table',4)">Drugs</th>
  </tr></thead><tbody></tbody></table></div>
</div>

<div id="drug-freq" class="panel">
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;">
    <input id="df-search" class="search-box" placeholder="Search drug name..." oninput="filterDrugFreqTable()" style="margin-bottom:0;max-width:220px;">
    <select id="df-filter-verdict" onchange="filterDrugFreqTable()" style="padding:8px;font-size:14px;border-radius:6px;border:1px solid #cbd5e0;">
      <option value="">Verdict: All</option>
      <option value="Verified">Verified</option>
      <option value="Unverified">Unverified</option>
      <option value="Not in Database">Not in DB</option>
    </select>
    <select id="df-filter-sel" onchange="filterDrugFreqTable()" style="padding:8px;font-size:14px;border-radius:6px;border:1px solid #cbd5e0;">
      <option value="">Selectivity: All</option>
      <option value="SELECTIVE">Selective</option>
      <option value="MODERATELY SELECTIVE">Mod. Selective</option>
      <option value="PROMISCUOUS">Promiscuous</option>
      <option value="UNKNOWN">Unknown</option>
      <option value="__BLANK__">No Data</option>
    </select>
    <select id="df-filter-alts" onchange="filterDrugFreqTable()" style="padding:8px;font-size:14px;border-radius:6px;border:1px solid #cbd5e0;">
      <option value="">Alternates: All</option>
      <option value="yes">Has Alternates</option>
      <option value="no">No Alternates</option>
    </select>
    <span id="df-count" style="font-size:13px;color:#666;margin-left:8px;"></span>
    <button onclick="downloadDrugFreqCSV()" style="padding:6px 14px;font-size:13px;border-radius:6px;border:1px solid #cbd5e0;background:#fff;cursor:pointer;margin-left:auto;">Download CSV</button>
  </div>
  <div class="table-wrap"><table id="df-table"><thead><tr>
    <th onclick="sortTable('df-table',0)">Drug</th>
    <th onclick="sortTable('df-table',1)">DB Match</th>
    <th onclick="sortTable('df-table',2)">Papers</th>
    <th onclick="sortTable('df-table',3)">Verified</th>
    <th onclick="sortTable('df-table',4)">Unverified</th>
    <th onclick="sortTable('df-table',5)">Not in DB</th>
    <th onclick="sortTable('df-table',6)">DB Target Gene</th>
    <th onclick="sortTable('df-table',7)" style="max-width:200px">Claim Targets</th>
    <th onclick="sortTable('df-table',8)">Selectivity</th>
  </tr></thead><tbody></tbody></table></div>
</div>

<div id="gene-freq" class="panel">
  <p style="font-size:13px;color:#666;margin:0 0 8px;">Gene counts based on claimed/intended targets in papers. &ldquo;Verified&rdquo; = papers where the drug&rsquo;s target claim was verified in the DB. &ldquo;Unverified&rdquo; = unverified claims. Paper counts exclude not-in-DB drugs; drug counts show them separately. Click a row to see which drugs target that gene.</p>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;">
    <input id="gf-search" class="search-box" placeholder="Search gene..." oninput="filterGeneFreqTable()" style="margin-bottom:0;max-width:220px;">
    <span id="gf-count" style="font-size:13px;color:#666;"></span>
  </div>
  <div class="table-wrap"><table id="gf-table"><thead><tr>
    <th onclick="sortGeneFreqTable(0)">Gene</th>
    <th onclick="sortGeneFreqTable(1)">Total Papers</th>
    <th onclick="sortGeneFreqTable(2)">Verified Papers</th>
    <th onclick="sortGeneFreqTable(3)">Unverified Papers</th>
    <th onclick="sortGeneFreqTable(4)">Total Drugs</th>
    <th onclick="sortGeneFreqTable(5)">Verified Drugs</th>
    <th onclick="sortGeneFreqTable(6)">Unverified Drugs</th>
    <th onclick="sortGeneFreqTable(7)">Not-in-DB Drugs</th>
  </tr></thead><tbody></tbody></table></div>
</div>

<div id="by-category" class="panel">
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;">
    <input id="cat-search-drug" class="search-box" placeholder="Search drug name..." oninput="filterDrugTable()" style="margin-bottom:0;max-width:220px;">
    <input id="cat-search-paper" class="search-box" placeholder="Search paper..." oninput="filterDrugTable()" style="margin-bottom:0;max-width:220px;">
    <select id="cat-filter-verdict" onchange="filterDrugTable()" style="padding:8px;font-size:14px;border-radius:6px;border:1px solid #cbd5e0;">
      <option value="">Verdict: All</option>
      <option value="Verified">Verified</option>
      <option value="Unverified">Unverified</option>
      <option value="Not in Database">Not in DB</option>
    </select>
    <select id="cat-filter-sel" onchange="filterDrugTable()" style="padding:8px;font-size:14px;border-radius:6px;border:1px solid #cbd5e0;">
      <option value="">Selectivity: All</option>
      <option value="SELECTIVE">Selective</option>
      <option value="MODERATELY SELECTIVE">Mod. Selective</option>
      <option value="PROMISCUOUS">Promiscuous</option>
      <option value="UNKNOWN">Unknown</option>
      <option value="__BLANK__">No Data</option>
    </select>
    <select id="cat-filter-alts" onchange="filterDrugTable()" style="padding:8px;font-size:14px;border-radius:6px;border:1px solid #cbd5e0;">
      <option value="">Alternates: All</option>
      <option value="yes">Has Alternates</option>
      <option value="no">No Alternates</option>
    </select>
    <span id="cat-count" style="font-size:13px;color:#666;margin-left:8px;"></span>
    <button onclick="downloadCategoryCSV()" style="padding:6px 14px;font-size:13px;border-radius:6px;border:1px solid #cbd5e0;background:#fff;cursor:pointer;margin-left:auto;">Download CSV</button>
  </div>
  <div class="table-wrap"><table id="cat-table"><thead><tr>
    <th onclick="sortTable('cat-table',0)">Drug</th>
    <th onclick="sortTable('cat-table',1)">DB Match</th>
    <th onclick="sortTable('cat-table',2)">Paper Target</th>
    <th onclick="sortTable('cat-table',3)">Paper Gene</th>
    <th onclick="sortTable('cat-table',4)">DB Target Gene</th>
    <th onclick="sortTable('cat-table',5)">Verdict</th>
    <th onclick="sortTable('cat-table',6)">Selectivity</th>
    <th onclick="sortTable('cat-table',7)">Alternates</th>
    <th onclick="sortTable('cat-table',8)">Paper</th>
  </tr></thead><tbody></tbody></table></div>
</div>

<div id="filter-log" class="panel">
  <details open>
    <summary>Stage 2 Rejections (S2 Type Filter)</summary>
    <div class="table-wrap"><table id="s2r-table"><thead><tr>
      <th onclick="sortTable('s2r-table',0)">Filename</th>
      <th onclick="sortTable('s2r-table',1)">Reason</th>
    </tr></thead><tbody></tbody></table></div>
  </details>
  <details>
    <summary>Stage 4 Rejections (Post-Gemini)</summary>
    <div class="table-wrap"><table id="fl-table"><thead><tr>
      <th onclick="sortTable('fl-table',0)">Filename</th>
      <th onclick="sortTable('fl-table',1)">Title</th>
      <th onclick="sortTable('fl-table',2)">Reasons</th>
      <th onclick="sortTable('fl-table',3)">Targeted Count</th>
    </tr></thead><tbody></tbody></table></div>
  </details>
</div>

<script>
const DATA = {data_json};

function esc(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}}

function showTab(id, btn) {{
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
}}

function populateTable(tableId, rows, cellFn) {{
  const tbody = document.querySelector('#' + tableId + ' tbody');
  tbody.innerHTML = '';
  rows.forEach(r => {{
    const tr = document.createElement('tr');
    cellFn(r).forEach(text => {{
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }});
    tbody.appendChild(tr);
  }});
}}

function sortTable(tableId, colIdx) {{
  const table = document.getElementById(tableId);
  const tbody = table.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const dir = table.dataset.sortDir === 'asc' ? 'desc' : 'asc';
  table.dataset.sortDir = dir;
  rows.sort((a, b) => {{
    let av = a.cells[colIdx].textContent;
    let bv = b.cells[colIdx].textContent;
    const an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return dir === 'asc' ? an - bn : bn - an;
    return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

function filterTable(tableId, query) {{
  const rows = document.querySelectorAll('#' + tableId + ' tbody tr');
  const q = query.toLowerCase();
  rows.forEach(r => {{
    r.style.display = r.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}

// Pre-group mentions by drug (case-insensitive)
const _drugGroups = (function() {{
  const mentions = {{}};
  const nameCounts = {{}};
  (DATA.all_drugs || []).forEach(r => {{
    const key = (r['DB Match'] || r['Drug'] || '').toLowerCase();
    if (!mentions[key]) {{ mentions[key] = []; nameCounts[key] = {{}}; }}
    mentions[key].push(r);
    const name = r['DB Match'] || r['Drug'] || '';
    nameCounts[key][name] = (nameCounts[key][name] || 0) + 1;
  }});
  const names = {{}};
  const variants = {{}};
  Object.keys(mentions).forEach(key => {{
    let best = '', bestCount = 0;
    for (const [name, cnt] of Object.entries(nameCounts[key])) {{
      if (cnt > bestCount) {{ best = name; bestCount = cnt; }}
    }}
    names[key] = best;
    variants[key] = Object.keys(nameCounts[key]);
  }});
  return {{ mentions, names, variants, totalDrugs: Object.keys(mentions).length }};
}})();

function _filterMention(r, selF, altsF) {{
  if (selF) {{
    const sv = (r['Selectivity'] || '').toUpperCase();
    if (selF === '__BLANK__') {{ if (sv !== 'NONE' && sv !== 'NULL' && sv !== '') return false; }}
    else if (sv !== selF) return false;
  }}
  if (altsF === 'yes' && !r['Alternate Compounds']) return false;
  if (altsF === 'no' && r['Alternate Compounds']) return false;
  return true;
}}

function filterDrugFreqTable() {{
  const drugQ = document.getElementById('df-search').value.toLowerCase();
  const verdictF = document.getElementById('df-filter-verdict').value;
  const selF = document.getElementById('df-filter-sel').value;
  const altsF = document.getElementById('df-filter-alts').value;
  const results = [];
  Object.keys(_drugGroups.mentions).forEach(key => {{
    if (drugQ && !key.includes(drugQ)) return;
    const raw = _drugGroups.mentions[key];
    const fm = (selF || altsF) ? raw.filter(r => _filterMention(r, selF, altsF)) : raw;
    if (fm.length === 0) return;
    // Apply verdict filter: only include matching mentions in counts and aggregation
    let vfm;
    if (verdictF === 'Verified') vfm = fm.filter(r => r['Verdict'] === 'Verified');
    else if (verdictF === 'Unverified') vfm = fm.filter(r => r['Verdict'] !== 'Verified' && r['Verdict'] !== 'Not in Database');
    else if (verdictF === 'Not in Database') vfm = fm.filter(r => r['Verdict'] === 'Not in Database');
    else vfm = fm;
    if (vfm.length === 0) return;
    // Count unique papers, not raw mentions
    const verifiedPapers = new Set(), unverifiedPapers = new Set(), notInDbPapers = new Set();
    const genes = new Set(), claims = new Set(), sels = new Set(), dbMatches = new Set();
    vfm.forEach(r => {{
      const paper = r['Paper'] || '';
      if (r['Verdict'] === 'Verified') verifiedPapers.add(paper);
      else if (r['Verdict'] === 'Not in Database') notInDbPapers.add(paper);
      else unverifiedPapers.add(paper);
      const gene = (r['DB Target Genes'] || '').trim();
      if (gene && gene !== 'None') gene.split(/[,;/]+/).forEach(g => {{ const gt = g.trim(); if (gt) genes.add(gt); }});
      const claim = (r['Paper Target'] || '').trim();
      if (claim && claim !== 'None') claims.add(claim);
      const sel = (r['Selectivity'] || '').toUpperCase();
      if (sel !== 'NONE' && sel !== 'NULL' && sel !== '' && sel !== 'UNKNOWN') sels.add(sel);
      const dbm = (r['DB Match'] || '').trim();
      if (dbm) dbMatches.add(dbm);
    }});
    const allPapers = new Set([...verifiedPapers, ...unverifiedPapers, ...notInDbPapers]);
    results.push({{ name: _drugGroups.names[key], key, count: allPapers.size, nVerified: verifiedPapers.size, nUnverified: unverifiedPapers.size, nNotInDb: notInDbPapers.size, genes, claims, sels, dbMatches }});
  }});
  results.sort((a, b) => b.count - a.count);
  const tbody = document.querySelector('#df-table tbody');
  tbody.innerHTML = '';
  results.forEach(d => {{
    const tr = document.createElement('tr');
    tr.className = 'freq-row';
    tr.innerHTML = '<td>' + esc(d.name) + '</td><td>' + esc([...d.dbMatches].sort().join(', ')) + '</td><td>' + d.count + '</td><td>' + d.nVerified + '</td><td>' + d.nUnverified + '</td><td>' + d.nNotInDb + '</td><td>' + esc([...d.genes].sort().join(', ')) + '</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc([...d.claims].sort().join(', ')) + '</td><td>' + esc([...d.sels].sort().join(', ')) + '</td>';
    tr.onclick = function() {{
      const next = tr.nextElementSibling;
      if (next && next.classList.contains('detail-row')) {{ next.remove(); return; }}
      tbody.querySelectorAll('.detail-row').forEach(x => x.remove());
      let papers = [];
      // Look up by both raw variant names and DB match names (drug_papers is keyed by freq_key = db_match or raw name)
      const lookupKeys = new Set([...(_drugGroups.variants[d.key] || []), ...d.dbMatches]);
      lookupKeys.forEach(v => {{ (DATA.drug_papers[v] || []).forEach(p => papers.push(p)); }});
      const detailRow = document.createElement('tr');
      detailRow.className = 'detail-row';
      const td = document.createElement('td');
      td.colSpan = 9;
      if (papers.length === 0) {{
        td.textContent = 'No paper details available';
      }} else {{
        const ul = document.createElement('ul');
        papers.forEach(p => {{
          const li = document.createElement('li');
          if (p.u) {{
            const a = document.createElement('a');
            a.href = p.u; a.target = '_blank'; a.rel = 'noopener';
            a.textContent = p.t || p.f;
            li.appendChild(a);
          }} else {{ li.textContent = p.t || p.f; }}
          ul.appendChild(li);
        }});
        td.appendChild(ul);
      }}
      detailRow.appendChild(td);
      tr.after(detailRow);
    }};
    tbody.appendChild(tr);
  }});
  document.getElementById('df-count').textContent = 'Showing ' + results.length + ' of ' + _drugGroups.totalDrugs;
  filterDrugFreqTable._lastFiltered = results;
}}

let _geneFreqData = null;
let _geneFreqFiltered = null;
let _geneFreqSortCol = 1;
let _geneFreqSortDir = 'desc';

function populateGeneFreqTable() {{
  const gf = DATA.gene_frequencies || [];
  _geneFreqData = gf.map(r => ({{
    gene: r[0], total: r[1], verified: r[2], unverified: r[3],
    totalDrugs: r[4] || 0, verifiedDrugs: r[5] || 0,
    unverifiedDrugs: r[6] || 0, notInDbDrugs: r[7] || 0
  }}));
  _geneFreqFiltered = _geneFreqData;
  _renderGeneFreqTable();
}}

function filterGeneFreqTable() {{
  const q = (document.getElementById('gf-search').value || '').toUpperCase();
  _geneFreqFiltered = q ? _geneFreqData.filter(r => r.gene.indexOf(q) !== -1) : _geneFreqData;
  _renderGeneFreqTable();
}}

function _renderGeneFreqTable() {{
  const gd = DATA.gene_drugs || {{}};
  const tbody = document.querySelector('#gf-table tbody');
  tbody.innerHTML = '';
  const sorted = [...(_geneFreqFiltered || _geneFreqData || [])];
  sorted.sort((a, b) => {{
    const keys = ['gene', 'total', 'verified', 'unverified', 'totalDrugs', 'verifiedDrugs', 'unverifiedDrugs', 'notInDbDrugs'];
    let av = a[keys[_geneFreqSortCol]], bv = b[keys[_geneFreqSortCol]];
    if (typeof av === 'number') return _geneFreqSortDir === 'asc' ? av - bv : bv - av;
    return _geneFreqSortDir === 'asc' ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  }});
  sorted.forEach(r => {{
    const tr = document.createElement('tr');
    tr.className = 'freq-row';
    tr.innerHTML = '<td>' + esc(r.gene) + '</td><td>' + r.total + '</td><td>' + r.verified + '</td><td>' + r.unverified + '</td><td>' + r.totalDrugs + '</td><td>' + r.verifiedDrugs + '</td><td>' + r.unverifiedDrugs + '</td><td>' + r.notInDbDrugs + '</td>';
    tr.onclick = function() {{
      const next = tr.nextElementSibling;
      if (next && next.classList.contains('detail-row')) {{ next.remove(); return; }}
      tbody.querySelectorAll('.detail-row').forEach(x => x.remove());
      const drugs = gd[r.gene] || [];
      const detailRow = document.createElement('tr');
      detailRow.className = 'detail-row';
      const td = document.createElement('td');
      td.colSpan = 8;
      if (drugs.length === 0) {{
        td.textContent = 'No drug details available';
      }} else {{
        const tbl = document.createElement('table');
        tbl.style.cssText = 'width:100%;font-size:12px;margin-top:4px;';
        tbl.innerHTML = '<thead><tr><th>Drug</th><th>Verdict</th><th>Selectivity</th><th>Papers</th></tr></thead>';
        const tb = document.createElement('tbody');
        drugs.forEach(d => {{
          const dtr = document.createElement('tr');
          const badgeCls = 'badge-' + esc(d.i || 'gray');
          const paperLinks = (d.papers || []).map(p => {{
            const rawLabel = (p.t || p.f || '').substring(0, 60) + ((p.t || p.f || '').length > 60 ? '...' : '');
            const label = esc(rawLabel);
            if (p.u) return '<a href="' + esc(p.u) + '" target="_blank" rel="noopener">' + label + '</a>';
            return label;
          }}).join('<br>');
          dtr.innerHTML = '<td>' + esc(d.d || '') + '</td><td><span class="badge ' + badgeCls + '">' + esc(d.v || '') + '</span></td><td>' + esc(d.s || '') + '</td><td>' + paperLinks + '</td>';
          tb.appendChild(dtr);
        }});
        tbl.appendChild(tb);
        td.appendChild(tbl);
      }}
      detailRow.appendChild(td);
      tr.after(detailRow);
    }};
    tbody.appendChild(tr);
  }});
  const total = (_geneFreqData || []).length;
  document.getElementById('gf-count').textContent = sorted.length < total ? 'Showing ' + sorted.length + ' of ' + total : total + ' genes';
}}

function sortGeneFreqTable(colIdx) {{
  if (_geneFreqSortCol === colIdx) {{
    _geneFreqSortDir = _geneFreqSortDir === 'asc' ? 'desc' : 'asc';
  }} else {{
    _geneFreqSortCol = colIdx;
    _geneFreqSortDir = colIdx === 0 ? 'asc' : 'desc';
  }}
  _renderGeneFreqTable();
}}

var _msFilters = {{ notInDb: false, gt5: false }};

function toggleMsFilter(key) {{
  _msFilters[key] = !_msFilters[key];
  var btnId = key === 'notInDb' ? 'ms-btn-notindb' : 'ms-btn-gt5';
  var btn = document.getElementById(btnId);
  if (_msFilters[key]) {{
    btn.style.background = '#3182ce';
    btn.style.color = '#fff';
    btn.style.borderColor = '#3182ce';
  }} else {{
    btn.style.background = '#fff';
    btn.style.color = '';
    btn.style.borderColor = '#cbd5e0';
  }}
  filterManuscriptTable();
}}

function filterManuscriptTable() {{
  var q = (document.getElementById('ms-search').value || '').toLowerCase();
  var all = DATA.manuscripts || [];
  var filtered = all.filter(function(r) {{
    if (q) {{
      var hay = ((r.Title||'') + ' ' + (r.Journal||'') + ' ' + (r.Author||'')).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }}
    if (_msFilters.notInDb && !(r.notInDbCount > 0)) return false;
    if (_msFilters.gt5 && !(r.drugCount > 5)) return false;
    return true;
  }});
  populateManuscriptTable(filtered);
  document.getElementById('ms-count').textContent = filtered.length + ' / ' + all.length + ' manuscripts';
}}

function populateManuscriptTable(rows) {{
  const tbody = document.querySelector('#ms-table tbody');
  tbody.innerHTML = '';
  rows.forEach(r => {{
    const tr = document.createElement('tr');
    tr.className = 'freq-row';
    const dc = r.drugCount || 0;
    const nidb = r.notInDbCount || 0;
    const drugLabel = nidb > 0 ? dc + ' (' + nidb + ' not in DB)' : '' + dc;
    tr.innerHTML = '<td>' + esc(r.Title || '') + '</td><td>' + esc(r.Journal || '') + '</td><td>' + esc(r.Year || '') + '</td><td>' + esc(r.Author || '') + '</td><td>' + esc(drugLabel) + '</td>';
    tr.onclick = function() {{
      const next = tr.nextElementSibling;
      if (next && next.classList.contains('detail-row')) {{
        next.remove();
        return;
      }}
      tbody.querySelectorAll('.detail-row').forEach(d => d.remove());
      const detail = DATA.paper_details[r.Filename] || {{}};
      const drugs = detail.d || [];
      const url = detail.u || '';
      const detailRow = document.createElement('tr');
      detailRow.className = 'detail-row';
      const td = document.createElement('td');
      td.colSpan = 5;
      // Paper link
      if (url) {{
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = 'View paper \u2197';
        link.style.cssText = 'display:inline-block;margin-bottom:8px;font-weight:600;';
        td.appendChild(link);
      }}
      if (drugs.length === 0) {{
        const p = document.createElement('p');
        p.textContent = 'No targeted drugs extracted';
        td.appendChild(p);
      }} else {{
        const tbl = document.createElement('table');
        tbl.style.cssText = 'width:100%;font-size:12px;margin-top:4px;';
        tbl.innerHTML = '<thead><tr><th>Drug</th><th>Target (Paper)</th><th>Verdict</th><th>Selectivity</th></tr></thead>';
        const tb = document.createElement('tbody');
        drugs.forEach(d => {{
          const dtr = document.createElement('tr');
          const badgeCls = 'badge-' + esc(d.i || 'gray');
          dtr.innerHTML = '<td>' + esc(d.n || '') + '</td><td>' + esc(d.pt || '') + '</td><td><span class="badge ' + badgeCls + '">' + esc(d.v || '') + '</span></td><td>' + esc(d.s || '') + '</td>';
          tb.appendChild(dtr);
        }});
        tbl.appendChild(tb);
        td.appendChild(tbl);
      }}
      detailRow.appendChild(td);
      tr.after(detailRow);
    }};
    tbody.appendChild(tr);
  }});
}}

function filterDrugTable() {{
  const drugQ = document.getElementById('cat-search-drug').value.toLowerCase();
  const paperQ = document.getElementById('cat-search-paper').value.toLowerCase();
  const verdictF = document.getElementById('cat-filter-verdict').value;
  const selF = document.getElementById('cat-filter-sel').value;
  const altsF = document.getElementById('cat-filter-alts').value;
  const all = DATA.all_drugs || [];
  const filtered = all.filter(r => {{
    if (drugQ && !(r['Drug'] || '').toLowerCase().includes(drugQ)) return false;
    if (paperQ) {{
      const p = (r['Paper'] || '').toLowerCase();
      const pt = (r['Paper Title'] || '').toLowerCase();
      if (!p.includes(paperQ) && !pt.includes(paperQ)) return false;
    }}
    if (verdictF && (r['Verdict'] || '') !== verdictF) return false;
    if (selF) {{
      const sv = (r['Selectivity'] || '').toUpperCase();
      if (selF === '__BLANK__') {{
        if (sv !== 'NONE' && sv !== 'NULL' && sv !== '') return false;
      }} else if (sv !== selF) return false;
    }}
    if (altsF === 'yes' && !r['Alternate Compounds']) return false;
    if (altsF === 'no' && r['Alternate Compounds']) return false;
    return true;
  }});
  populateTable('cat-table', filtered, r => [
    r['Drug'] || '', r['DB Match'] || '', r['Paper Target'] || '', r['Human Target Gene'] || '', r['DB Target Genes'] || '',
    r['Verdict'] || '', r['Selectivity'] || '', r['Alternate Compounds'] || '', r['Paper'] || ''
  ]);
  document.getElementById('cat-count').textContent = 'Showing ' + filtered.length + ' of ' + all.length;
  filterDrugTable._lastFiltered = filtered;
}}

function _csvEscape(val) {{
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\\n')) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}}

function _buildFilterSuffix(ids) {{
  const parts = [];
  ids.forEach(id => {{
    const el = document.getElementById(id);
    if (el && el.value) {{
      const v = el.value === '__BLANK__' ? 'no_data' : el.value.toLowerCase().replace(/[^a-z0-9]+/g, '_');
      parts.push(v);
    }}
  }});
  return parts.length ? '_' + parts.join('_') : '_all';
}}

function _downloadFile(filename, csvContent) {{
  const blob = new Blob([csvContent], {{ type: 'text/csv;charset=utf-8;' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}}

function downloadDrugFreqCSV() {{
  const data = filterDrugFreqTable._lastFiltered || [];
  const headers = ['Drug', 'DB Match', 'Papers', 'Verified', 'Unverified', 'DB Target Gene', 'Claim Targets', 'Selectivity'];
  const rows = data.map(d => [
    d.name, [...d.dbMatches].sort().join('; '), d.count, d.nVerified, d.nUnverified, [...d.genes].sort().join('; '), [...d.claims].sort().join('; '), [...d.sels].sort().join('; ')
  ]);
  const csv = [headers.join(',')].concat(rows.map(r => r.map(_csvEscape).join(','))).join('\\n');
  const suffix = _buildFilterSuffix(['df-filter-verdict', 'df-filter-sel', 'df-filter-alts']);
  _downloadFile('drug_frequency' + suffix + '.csv', csv);
}}

function downloadCategoryCSV() {{
  const data = filterDrugTable._lastFiltered || [];
  const headers = ['Drug', 'DB Match', 'Paper Target', 'Human Target Gene', 'DB Target Genes', 'Verdict', 'Selectivity', 'Alternate Compounds', 'Paper'];
  const rows = data.map(r => [
    r['Drug'] || '', r['DB Match'] || '', r['Paper Target'] || '', r['Human Target Gene'] || '', r['DB Target Genes'] || '',
    r['Verdict'] || '', r['Selectivity'] || '', r['Alternate Compounds'] || '', r['Paper'] || ''
  ]);
  const csv = [headers.join(',')].concat(rows.map(r => r.map(_csvEscape).join(','))).join('\\n');
  const suffix = _buildFilterSuffix(['cat-filter-verdict', 'cat-filter-sel', 'cat-filter-alts']);
  _downloadFile('by_category' + suffix + '.csv', csv);
}}

// ---- Summary page init ----
const S = DATA.summary;
const PS = DATA.paper_stats;

function _pct(n, d) {{ return d ? Math.round(n / d * 100) : 0; }}
function _statText(n, d) {{ return n + ' / ' + d + ' (' + _pct(n, d) + '%)'; }}

// Funnel
(function() {{
  const fc = document.getElementById('funnel-container');
  if (!DATA.funnel || !DATA.funnel.length) return;
  const maxVal = DATA.funnel[0][1] || 1;
  // Use CSS grid so label and bar columns are fixed-width
  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:220px 1fr;gap:3px 0;align-items:center;';
  DATA.funnel.forEach(([label, count], i) => {{
    const pct = Math.max(Math.round(count / maxVal * 100), 8);
    const isLast = i === DATA.funnel.length - 1;
    const lbl = document.createElement('div');
    lbl.style.cssText = 'font-size:13px;color:#4a5568;text-align:right;padding-right:12px;white-space:nowrap;';
    lbl.textContent = label;
    const bar = document.createElement('div');
    bar.style.cssText = 'background:' + (isLast ? '#48bb78' : '#4299e1') + ';height:26px;border-radius:4px;width:' + pct + '%;min-width:40px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;font-size:13px;';
    bar.textContent = count.toLocaleString();
    grid.appendChild(lbl);
    grid.appendChild(bar);
  }});
  fc.appendChild(grid);
}})();

// Drug-Paper Pairs breakdown
(function() {{
  const cc = S.category_counts;
  const total = S.total_drug_paper_pairs;
  document.getElementById('stat-pairs-total').textContent = total.toLocaleString();

  const groups = [
    {{ label: 'Verified Selective', keys: ['verified_selective'], color: '#48bb78', badge: 'badge-green' }},
    {{ label: 'Verified Mod. Selective', keys: ['verified_mod_selective'], color: '#68d391', badge: 'badge-green' }},
    {{ label: 'Verified Promiscuous', keys: ['verified_promiscuous'], color: '#ecc94b', badge: 'badge-yellow' }},
    {{ label: 'Verified Unknown Sel.', keys: ['verified_unknown'], color: '#f6e05e', badge: 'badge-yellow' }},
    {{ label: 'Unverified', keys: ['unverified'], color: '#f56565', badge: 'badge-red' }},
    {{ label: 'Not in DB', keys: ['not_in_db'], color: '#a0aec0', badge: 'badge-gray' }},
  ];

  // Stacked bar
  const barContainer = document.getElementById('pairs-bar');
  const barDiv = document.createElement('div');
  barDiv.style.cssText = 'display:flex;height:32px;border-radius:6px;overflow:hidden;margin-top:8px;';
  groups.forEach(g => {{
    const count = g.keys.reduce((s, k) => s + (cc[k] || 0), 0);
    if (count === 0) return;
    const seg = document.createElement('div');
    seg.style.cssText = 'height:100%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:600;min-width:20px;';
    seg.style.width = (count / total * 100) + '%';
    seg.style.background = g.color;
    seg.title = g.label + ': ' + count.toLocaleString();
    if (count / total > 0.05) seg.textContent = _pct(count, total) + '%';
    barDiv.appendChild(seg);
  }});
  barContainer.appendChild(barDiv);

  // Legend row under bar
  const legend = document.createElement('div');
  legend.style.cssText = 'display:flex;flex-wrap:wrap;gap:12px;margin-top:6px;';
  groups.forEach(g => {{
    const count = g.keys.reduce((s, k) => s + (cc[k] || 0), 0);
    const item = document.createElement('span');
    item.style.cssText = 'font-size:11px;color:#4a5568;display:flex;align-items:center;gap:4px;';
    item.innerHTML = '<span style="width:10px;height:10px;border-radius:2px;background:' + g.color + ';display:inline-block"></span>' + g.label;
    legend.appendChild(item);
  }});
  barContainer.appendChild(legend);

  // Table
  const tbody = document.querySelector('#cat-summary-table tbody');
  let runTotal = 0;
  groups.forEach(g => {{
    const count = g.keys.reduce((s, k) => s + (cc[k] || 0), 0);
    runTotal += count;
    const tr = document.createElement('tr');
    tr.innerHTML = '<td><span class="badge ' + g.badge + '">' + g.label + '</span></td>' +
      '<td style="text-align:right;font-weight:600">' + count.toLocaleString() + '</td>' +
      '<td style="text-align:right;color:#718096">' + _pct(count, total) + '%</td>' +
      '<td><div style="background:' + g.color + ';height:12px;border-radius:2px;width:' + Math.max(count/total*200, 2) + 'px"></div></td>';
    tbody.appendChild(tr);
  }});
  const tr = document.createElement('tr');
  tr.style.borderTop = '2px solid #e2e8f0';
  tr.innerHTML = '<td style="font-weight:700">Total</td><td style="text-align:right;font-weight:700">' + runTotal.toLocaleString() + '</td><td></td><td></td>';
  tbody.appendChild(tr);
}})();

// Unique drugs
document.getElementById('stat-unique').textContent = S.unique_drugs.toLocaleString();
document.getElementById('stat-drugs-indb').textContent = PS.unique_drugs_in_db.toLocaleString();
document.getElementById('stat-drugs-notindb').textContent = PS.unique_drugs_not_in_db.toLocaleString();

// Paper coverage
document.getElementById('stat-papers-uv').textContent = _statText(PS.papers_with_unverified, PS.total_papers);
document.getElementById('stat-papers-alt').textContent = _statText(PS.papers_with_actionable_alt, PS.total_papers);

filterManuscriptTable();
filterDrugFreqTable();
populateGeneFreqTable();

populateTable('s2r-table', DATA.s2_rejected, r => [r.filename || '', r.reason || '']);
populateTable('fl-table', DATA.filter_log, r => [
  r.filename || '', r.title || '', (r.reasons || []).join(', '), r.targeted_count || ''
]);

filterDrugTable();
</script>
</body>
</html>"""

    html_path = os.path.join(report_dir, "pipeline_report.html")
    with open(html_path, "w") as f:
        f.write(html)
    logger.info("HTML report: %s", html_path)


# ---------------------------------------------------------------------------
# Main pipeline orchestrator
# ---------------------------------------------------------------------------

def _check_valet_db_populated() -> int:
    """Return the number of compound reports in the configured VALET database.

    Raises RuntimeError when the database has no reports, so Paper Query never
    spends Vertex AI batch money only to label every compound 'Not in Database'.
    """
    Session = init_db()
    session = Session()
    try:
        count = session.query(AnalysisResult).count()
    finally:
        session.close()
    if count == 0:
        raise RuntimeError(
            f"The VALET database ({DATABASE_URL}) has no compound reports. "
            "Run simple_batch.py first, point DATABASE_URL at a populated SQLite database, "
            "or pass --allow_empty_db to run extraction without database matches."
        )
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Multi-stage batch paper analysis pipeline."
    )
    parser.add_argument("folder", help="Directory containing PDF files to process.")
    parser.add_argument(
        "--poll_interval", type=int, default=60,
        help="Seconds between batch status checks.",
    )
    parser.add_argument(
        "--year_cutoff", type=int, default=2024,
        help="Exclude papers before this year.",
    )
    parser.add_argument(
        "--min_targeted_drugs", type=int, default=0,
        help="Minimum targeted drugs per paper to keep.",
    )
    parser.add_argument(
        "--force_stage",
        choices=list(FORCE_STAGE_MAP.keys()),
        default=None,
        help="Re-run from a specific stage.",
    )
    parser.add_argument(
        "--dry_run", action="store_true",
        help="Show what would happen without executing or writing files.",
    )
    parser.add_argument(
        "--allow_empty_db", action="store_true",
        help="Allow running even when the VALET database has no compound reports yet.",
    )
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        logger.error("Folder does not exist: %s", folder)
        sys.exit(1)

    def save(state):
        # A dry run never saves its progress, so a later real run starts where it would have.
        if not args.dry_run:
            save_state(folder, state)

    # Load / reset state
    state = load_state(folder)
    if args.force_stage:
        forced = FORCE_STAGE_MAP[args.force_stage]
        logger.info("Forcing pipeline back to stage: %s (from %s)", forced, state["stage"])
        state["stage"] = forced
        save(state)

    # Stop now, with a clear message, if a stage that calls Gemini will run and Gemini
    # cannot be used with these settings (or the VALET database has no reports to match against).
    if not args.dry_run and stage_index(state["stage"]) <= stage_index("gemini_parsed"):
        try:
            if not (USE_VERTEX_AI and GCS_BUCKET_NAME):
                raise GeminiSetupError(
                    "Paper Query sends the papers to Gemini in a batch job on Vertex AI. In the .env "
                    "file, set GOOGLE_CLOUD_PROJECT and GCS_BUCKET_NAME. See README: Settings.")
            check_gemini_setup([MODEL_NAME, TARGET_MATCH_MODEL])
        except GeminiSetupError as e:
            logger.error("%s", e)
            sys.exit(1)
        if not args.allow_empty_db:
            try:
                _check_valet_db_populated()
            except Exception as e:
                logger.error("%s", e)
                sys.exit(1)

    out_dir = results_dir(folder)
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)
    paper_results = []

    # ---- Stage 1: Parse ----
    if stage_index(state["stage"]) <= stage_index("init"):
        logger.info("=== Stage 1: Filename Parse + Year Filter ===")
        papers = stage1_parse(folder, args.year_cutoff, args.dry_run)
        if not papers:
            logger.error("No papers passed Stage 1. Exiting.")
            sys.exit(1)
        state["stage"] = "parsed"
        save(state)
    else:
        parsed_path = os.path.join(out_dir, "stage1_parsed.json")
        with open(parsed_path, "r") as f:
            papers = json.load(f)
        logger.info("Stage 1: Loaded %d papers from cache", len(papers))

    # ---- Stage 2: S2 Lookup ----
    if stage_index(state["stage"]) <= stage_index("parsed"):
        logger.info("=== Stage 2: Semantic Scholar Lookup + Type Filter ===")
        papers = stage2_s2_lookup(folder, papers, args.dry_run)
        if not papers:
            logger.error("No papers passed Stage 2. Exiting.")
            sys.exit(1)
        state["stage"] = "s2_checked"
        save(state)
    else:
        s2_path = os.path.join(out_dir, "stage2_s2_results.json")
        with open(s2_path, "r") as f:
            papers = json.load(f)
        logger.info("Stage 2: Loaded %d papers from cache", len(papers))

    # ---- Stage 3a: Submit ----
    no_new_papers = False
    if stage_index(state["stage"]) <= stage_index("s2_checked"):
        logger.info("=== Stage 3a: PDF Upload + Batch Submit ===")
        job_id = stage3_submit(folder, papers, state, args.dry_run)
        if not job_id:
            logger.error("Failed to submit batch job. Exiting.")
            sys.exit(1)
        if job_id == "no_new_papers":
            # All papers already have results — skip poll/download, go to extract
            no_new_papers = True
            state["stage"] = "polled"
            save(state)
        else:
            state["job_id"] = job_id
            state["stage"] = "submitted"
            save(state)
    else:
        job_id = state.get("job_id")

    # ---- Stage 3b: Poll ----
    if not no_new_papers and stage_index(state["stage"]) <= stage_index("submitted"):
        logger.info("=== Stage 3b: Polling Batch Job ===")
        if not stage3_poll(job_id, args.poll_interval, args.dry_run):
            logger.error("Batch job did not succeed. Exiting.")
            sys.exit(1)
        state["stage"] = "polled"
        save(state)

    # ---- Stage 3c: Download + Parse ----
    if stage_index(state["stage"]) <= stage_index("polled"):
        logger.info("=== Stage 3c: Download + Parse Results ===")
        raw_path = None if no_new_papers else stage3_download(folder, job_id, args.dry_run)
        if args.dry_run:
            logger.info("[DRY RUN] Skipping parse")
            state["stage"] = "extracted"
            save(state)
            paper_results = []
        elif no_new_papers:
            # No new batch — existing gemini_parsed cache is already up to date
            logger.info("No new papers to parse.")
            state["stage"] = "gemini_parsed"
            save(state)
        else:
            if not raw_path:
                logger.error("Failed to download results. Exiting.")
                sys.exit(1)
            stage3_parse(folder, papers, raw_path)
            state["stage"] = "gemini_parsed"
            save(state)

    # ---- Stage 3d: Verify ----
    if args.dry_run:
        if stage_index(state["stage"]) <= stage_index("gemini_parsed"):
            logger.info("[DRY RUN] Skipping verify")
            state["stage"] = "extracted"
    elif stage_index(state["stage"]) <= stage_index("gemini_parsed"):
        logger.info("=== Stage 3d: Verify Drugs Against DB ===")
        # Migration: if stage3_gemini_parsed.json doesn't exist but raw JSONL does,
        # auto-run parse first (supports --force_stage verify on older batches)
        gemini_parsed_path = os.path.join(out_dir, "stage3_gemini_parsed.json")
        if not os.path.exists(gemini_parsed_path):
            raw_path = os.path.join(out_dir, "stage3_raw_results.jsonl")
            if os.path.exists(raw_path):
                logger.info("No gemini_parsed cache found, re-parsing from raw JSONL first")
                stage3_parse(folder, papers, raw_path)
            else:
                logger.error("No stage3_gemini_parsed.json or stage3_raw_results.jsonl found. Exiting.")
                sys.exit(1)
        paper_results = stage3_verify(folder)
        state["stage"] = "extracted"
        save(state)
    else:
        parsed_path = os.path.join(out_dir, "stage3_parsed.json")
        with open(parsed_path, "r") as f:
            paper_results = json.load(f)
        logger.info("Stage 3: Loaded %d verified results from cache", len(paper_results))

    # ---- Stage 4: Filter ----
    if args.dry_run:
        if stage_index(state["stage"]) <= stage_index("extracted"):
            logger.info("[DRY RUN] Skipping filter")
            state["stage"] = "filtered"
    elif stage_index(state["stage"]) <= stage_index("extracted"):
        logger.info("=== Stage 4: Post-Gemini Filtering ===")
        paper_results = stage4_filter(folder, paper_results, args.min_targeted_drugs)
        state["stage"] = "filtered"
        save(state)
    else:
        filtered_path = os.path.join(out_dir, "stage4_filtered.json")
        with open(filtered_path, "r") as f:
            paper_results = json.load(f)
        logger.info("Stage 4: Loaded %d filtered results from cache", len(paper_results))

    # ---- Stage 5: Reports ----
    if args.dry_run:
        if stage_index(state["stage"]) <= stage_index("filtered"):
            logger.info("[DRY RUN] Skipping report generation")
    elif stage_index(state["stage"]) <= stage_index("filtered"):
        logger.info("=== Stage 5: Report Generation ===")
        stage5_reports(folder, paper_results)
        state["stage"] = "reported"
        save(state)

    # ---- Summary ----
    print("\n" + "=" * 60)
    print("  PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Papers analyzed:     {len(paper_results)}")
    total_drugs = sum(len(p.get("results", [])) for p in paper_results)
    print(f"  Total drug mentions: {total_drugs}")
    print(f"  Reports directory:   {os.path.join(out_dir, 'stage5_reports')}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
