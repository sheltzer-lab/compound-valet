"""
Main Reference Checker Logic
"""

import asyncio
import aiohttp
import logging
import re
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional, Tuple

from core.references.settings import get_config
from core.references.clients import SemanticScholarClient, CrossRefClient
from core.references.utils import (
    is_safe_url,
    normalize_text,
    clean_title_for_search
)

logger = logging.getLogger(__name__)

# Similarity threshold for fuzzy title matching (same as original refchecker)
SIMILARITY_THRESHOLD = 0.8


def _year_int(value) -> Optional[int]:
    """The four-digit year at the start of `value` ("2020", 2020, "2020a" -> 2020), else None."""
    m = re.match(r"\s*(\d{4})", str(value)) if value is not None else None
    return int(m.group(1)) if m else None


def _errors_as_unmatched(results, indices, references, phase: str) -> list:
    """One reference's error must not fail the whole batch: count it as unmatched."""
    out = []
    for idx, result in zip(indices, results):
        if isinstance(result, BaseException):
            logger.warning(f"{phase} check failed for '{references[idx].get('title', '')[:80]}': {result!r}")
            result = None
        out.append(result)
    return out

class ReferenceChecker:
    def __init__(self):
        self.config = get_config()
        self.ss_client = SemanticScholarClient()
        self.cr_client = CrossRefClient()

    async def verify_references_batch(self, references: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Verify a batch of references using bulk API queries.
        Returns a list of verification results (dictionaries).
        """
        # Map to store results by index: {original_index: result}
        results_map = {}
        
        # Use timeout to prevent hanging connections
        timeout = aiohttp.ClientTimeout(total=60, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            
            # --- 1. Semantic Scholar Bulk Verification ---
            # Get all titles
            all_titles = [ref.get('title', '') for ref in references]
            
            # Filter out empty titles (keep index alignment by checking original list)
            searchable_titles = [t for t in all_titles if t and len(t.strip()) > 5]
            
            if searchable_titles:
                logger.info(
                    f"RefChecker Phase 1: querying Semantic Scholar bulk for "
                    f"{len(searchable_titles)} titles"
                )
                ss_candidates = await self.ss_client.search_paper_bulk(searchable_titles, session)

                # Match candidates to references
                for i, ref in enumerate(references):
                    if i in results_map: continue

                    match = self._find_best_match(ref, ss_candidates, source="semantic_scholar")
                    if match:
                        # [NEW] Quality Control: Ensure authors are present
                        # S2 sometimes returns papers with empty authors (e.g. for commentary).
                        # If we accept these, we lose the chance to find better metadata via CrossRef.
                        metadata = self._extract_metadata_ss(match)
                        if not metadata.get('authors'):
                            logger.debug(f"S2 match for '{ref.get('title')}' has no authors, trying CrossRef")
                            continue

                        safe_url = await self._resolve_best_url(match, "semantic_scholar", session)
                        if safe_url:
                            results_map[i] = self._create_result(
                                ref, verified=True, url=safe_url, source="Semantic Scholar", verified_match=metadata
                            )
                logger.info(
                    f"RefChecker Phase 1: Semantic Scholar matched "
                    f"{len(results_map)}/{len(references)} references"
                )

            # --- 3. CrossRef / Fallback Verification ---
            # For any remaining, fall back to individual checks (CrossRef or detailed S2 if needed)
            remaining_indices = [i for i in range(len(references)) if i not in results_map]
            if remaining_indices:
                logger.info(
                    f"RefChecker Phase 2: querying CrossRef individually for "
                    f"{len(remaining_indices)} remaining references"
                )

                # Create tasks for remaining
                tasks = []
                for i in remaining_indices:
                    tasks.append(self._check_crossref(references[i], session))

                # Run concurrently
                cr_results = await asyncio.gather(*tasks, return_exceptions=True)
                cr_results = _errors_as_unmatched(cr_results, remaining_indices, references, "CrossRef")

                # Assign CrossRef successes
                cr_matches = 0
                for idx, result in zip(remaining_indices, cr_results):
                    if result and result.get('verified'):
                        results_map[idx] = result
                        cr_matches += 1
                logger.info(
                    f"RefChecker Phase 2: CrossRef matched {cr_matches}/{len(remaining_indices)} "
                    f"remaining references"
                )

                # --- Phase 3: Truncated title fallback for long titles ---
                still_unmatched = [i for i in remaining_indices if i not in results_map]
                long_unmatched = [i for i in still_unmatched
                                  if len(references[i].get('title', '')) > 100]
                if long_unmatched:
                    logger.info(
                        f"RefChecker Phase 3: truncated-title fallback for "
                        f"{len(long_unmatched)} long-title references"
                    )
                    trunc_tasks = [self._check_crossref_truncated(references[i], session)
                                   for i in long_unmatched]
                    trunc_results = await asyncio.gather(*trunc_tasks, return_exceptions=True)
                    trunc_results = _errors_as_unmatched(trunc_results, long_unmatched, references,
                                                         "truncated-title CrossRef")
                    trunc_matches = 0
                    for idx, result in zip(long_unmatched, trunc_results):
                        if result and result.get('verified'):
                            results_map[idx] = result
                            trunc_matches += 1
                    logger.info(
                        f"RefChecker Phase 3: truncated-title matched "
                        f"{trunc_matches}/{len(long_unmatched)}"
                    )

                # Mark remaining failures
                for i in remaining_indices:
                    if i not in results_map:
                        results_map[i] = self._create_result(
                            references[i], verified=False, error="Not found in any database"
                        )
        
        # Reassemble results in order
        final_results = []
        for i in range(len(references)):
            if i in results_map:
                final_results.append(results_map[i])
            else:
                # Should have been caught by "Not found" above, but just in case
                final_results.append(self._create_result(references[i], verified=False, error="Skipped or Error"))
                
        return final_results

    def _find_best_match(self, reference: Dict[str, Any], candidates: List[Dict[str, Any]], source: str) -> Optional[Dict[str, Any]]:
        """Find best match in candidates pool based on Title ONLY"""
        ref_title = reference.get('title', '')
        if not ref_title:
            return None
            
        best_match = None
        best_score = 0.0
        
        for cand in candidates:
            # Extract candidate title based on source
            if source == "semantic_scholar":
                cand_title = cand.get('title')
            elif source == "crossref":
                t = cand.get('title', [])
                cand_title = t[0] if t else ''
            else:
                cand_title = ''
                
            score = self._title_similarity(ref_title, cand_title)
            
            if score > best_score:
                best_score = score
                best_match = cand
                
        if best_score >= SIMILARITY_THRESHOLD:
            return best_match
            
        return None

    def _truncate_title_at_separator(self, title: str) -> Optional[str]:
        """Truncate a long title at a natural separator (colon, dash, em-dash).
        Returns None if title is ≤100 chars (no truncation needed).
        """
        if not title or len(title) <= 100:
            return None

        separators = [': ', ' - ', ' — ']
        best_pos = -1
        for sep in separators:
            pos = title.rfind(sep)
            # Only use if it leaves at least 30 chars
            if pos >= 30 and (best_pos == -1 or pos > best_pos):
                best_pos = pos

        if best_pos > 0:
            return title[:best_pos].strip()

        # No separator found — hard-truncate at 100 chars on a word boundary
        truncated = title[:100]
        space_pos = truncated.rfind(' ')
        if space_pos >= 30:
            return truncated[:space_pos].strip()
        return truncated.strip()

    async def _check_crossref_truncated(self, reference: Dict[str, Any], session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Truncated title fallback: re-query CrossRef with the main title (before subtitle separator)."""
        title = reference.get('title', '')
        truncated = self._truncate_title_at_separator(title)
        if not truncated:
            return None

        clean_title = clean_title_for_search(truncated)
        year = reference.get('year')

        results = await self.cr_client.search_works(clean_title, session, year=year)

        # Compare against truncated title, not the full title
        trunc_ref = dict(reference, title=truncated)
        match = self._find_best_match(trunc_ref, results, source="crossref")

        if match:
            metadata = self._extract_metadata_cr(match)
            if not metadata.get('authors'):
                return None

            # Extra validation: require year or author corroboration
            ref_year = reference.get('year')
            ref_authors = reference.get('authors')
            has_year = ref_year is not None
            has_authors = bool(ref_authors)

            if has_year or has_authors:
                year_ok = True
                author_ok = True

                if has_year:
                    match_year = metadata.get('year')
                    ref_y, match_y = _year_int(ref_year), _year_int(match_year)
                    year_ok = ref_y is not None and ref_y == match_y   # "N/A" never corroborates

                if has_authors:
                    # Check if at least one author last name matches
                    ref_last_names = set()
                    if isinstance(ref_authors, list):
                        for a in ref_authors:
                            name = a if isinstance(a, str) else a.get('name', '')
                            parts = name.strip().split()
                            if parts:
                                ref_last_names.add(parts[-1].lower())
                    match_last_names = set()
                    for a in metadata.get('authors', []):
                        parts = a.strip().split()
                        if parts:
                            match_last_names.add(parts[-1].lower())
                    author_ok = bool(ref_last_names & match_last_names)

                if not (year_ok and author_ok):
                    logger.debug(f"Truncated match rejected for '{truncated}': year_ok={year_ok}, author_ok={author_ok}")
                    return None

            safe_url = await self._resolve_best_url(match, "crossref", session)
            if safe_url:
                return self._create_result(
                    reference, verified=True, url=safe_url, source="CrossRef", verified_match=metadata
                )
        return None

    async def _check_crossref(self, reference: Dict[str, Any], session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Check CrossRef for the reference"""
        title = reference.get('title', '')
        clean_title = clean_title_for_search(title)
        year = reference.get('year')
        
        # Search
        results = await self.cr_client.search_works(clean_title, session, year=year)
        
        # Match using the same pool logic (but here pool is just for this paper)
        match = self._find_best_match(reference, results, source="crossref")
        
        if match:
            # [NEW] Quality Control: Ensure authors are present (same as S2)
            metadata = self._extract_metadata_cr(match)
            if not metadata.get('authors'):
                # logger.warning(f"CrossRef match found for '{title}' but HAS NO AUTHORS. Rejecting.")
                return None

            safe_url = await self._resolve_best_url(match, "crossref", session)
            if safe_url:
                return self._create_result(
                    reference, verified=True, url=safe_url, source="CrossRef", verified_match=metadata
                )
        return None

    def _title_similarity(self, title1: str, title2: str) -> float:
        """Calculate similarity ratio between two titles using SequenceMatcher"""
        if not title1 or not title2:
            return 0.0
        # Normalize both titles
        t1 = normalize_text(title1)
        t2 = normalize_text(title2)
        return SequenceMatcher(None, t1, t2).ratio()

    # Removed old _is_match as we use _find_best_match now

    def _extract_metadata_ss(self, paper: Dict[str, Any]) -> Dict[str, Any]:
        """Extract standardized metadata from Semantic Scholar result"""
        # Return simple list of author names
        authors = [a.get('name', '') for a in paper.get('authors', []) if a.get('name')]
        # Calculate citation for DB
        # Format: Lastname, Year
        first_author_last = None
        if authors and authors[0]:
            first_author_last = authors[0].split()[-1]
            
        year = paper.get('year')
        title = paper.get('title') or "Untitled"
        
        if first_author_last and year:
            citation = f"{first_author_last}, {year}"
        elif first_author_last:
            citation = first_author_last
        else:
            # Fallback to title
            citation = title[:50] + "..." if len(title) > 50 else title

        return {
            'title': title,
            'authors': authors,
            'year': paper.get('year'),
            'citation': citation,
            'doi': (paper.get('externalIds') or {}).get('DOI'),
        }



    def _extract_metadata_cr(self, work: Dict[str, Any]) -> Dict[str, Any]:
        """Extract standardized metadata from CrossRef result"""
        authors = []
        for a in work.get('author', []):
            name = f"{a.get('given', '')} {a.get('family', '')}".strip()
            if name:
                authors.append(name)
                
        title_list = work.get('title', [])
        title = title_list[0] if title_list else 'Untitled'
        
        # Date parsing for CrossRef
        year = None
        published = work.get('published') or work.get('published-print') or work.get('published-online')
        if published and 'date-parts' in published:
            parts = published['date-parts']
            if parts and parts[0]:
                year = parts[0][0]
                
        # Calculate citation
        first_author_last = None
        if authors and authors[0]:
            first_author_last = authors[0].split()[-1]
            
        if first_author_last and year:
            citation = f"{first_author_last}, {year}"
        elif first_author_last:
            citation = first_author_last
        else:
            # Fallback to title
            citation = title[:50] + "..." if len(title) > 50 else title
                
        return {
            'title': title,
            'authors': authors,
            'year': year,
            'citation': citation,
            'doi': work.get('DOI'),
        }

    async def _verify_url_exists(self, session: aiohttp.ClientSession, url: str) -> Tuple[bool, Optional[str]]:
        """
        Verify if a URL exists (200 OK) and is safe.
        Returns (is_valid, final_url).
        """
        if not url or not is_safe_url(url):
            return False, None
            
        try:
            # Try HEAD first
            async with session.head(url, allow_redirects=True, timeout=10) as resp:
                if resp.status == 200:
                   final_url = str(resp.url)
                   if is_safe_url(final_url):
                       return True, final_url
                   return False, None
                elif resp.status in [405, 403, 401]:
                   # Method Not Allowed or Forbidden - try GET
                   pass
                else:
                   return False, None

            # Fallback to GET
            async with session.get(url, allow_redirects=True, timeout=15) as resp:
                if resp.status == 200:
                    final_url = str(resp.url)
                    if is_safe_url(final_url):
                        return True, final_url
                    return False, None
                return False, None
        except Exception as e:
            # logger.warning(f"Failed to verify URL {url}: {e}")
            return False, None

    async def _resolve_best_url(self, paper: Dict[str, Any], source: str, session: aiohttp.ClientSession) -> Optional[str]:
        """
        Resolve the best available URL for a paper, prioritizing:
        1. Open Access PDF
        2. DOI (Verified)
        3. Generic URL
        4. PubMed (Fallback)
        """
        candidates = []
        
        # 1. Open Access PDF
        if source == "semantic_scholar" and paper.get('openAccessPdf'):
            candidates.append(('oapdf', paper['openAccessPdf'].get('url')))
            
        # 2. DOI
        doi = None
        if source == "semantic_scholar":
             ids = paper.get('externalIds', {})
             doi = ids.get('DOI')
        elif source == "crossref":
             doi = paper.get('DOI')
             
        if doi:
            candidates.append(('doi', f"https://doi.org/{doi}"))
            
        # 3. Generic URL
        if source == "semantic_scholar":
            if paper.get('url'):
                candidates.append(('url', paper['url']))
        elif source == "crossref":
            if paper.get('URL'):
                 candidates.append(('url', paper['URL']))
            for link in paper.get('link', []):
                if link.get('URL'):
                    candidates.append(('url', link['URL']))

        # 4. PubMed (Last Resort)
        if source == "semantic_scholar":
             ids = paper.get('externalIds', {})
             if ids.get('PubMed'):
                 candidates.append(('pubmed', f"https://pubmed.ncbi.nlm.nih.gov/{ids['PubMed']}/"))
        
        # Iterate and verify
        for type_, url in candidates:
            if not url or not is_safe_url(url):
                continue
                
            # If it's a DOI, we MUST verify it exists and doesn't 404
            if type_ == 'doi':
                exists, final_url = await self._verify_url_exists(session, url)
                if exists:
                    return final_url
                else:
                    # logger.info(f"DOI failed verification: {url}")
                    continue
            
            # Only DOIs are checked for a live page. Other links are used as they are,
            # once is_safe_url accepts them.
            if is_safe_url(url):
                return url
                
        return None

    def _create_result(self, reference: Dict[str, Any], verified: bool, url: Optional[str] = None, source: Optional[str] = None, verified_match: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> Dict[str, Any]:
        """Create standardized result dictionary"""
        return {
            "reference": reference,
            "verified": verified,
            "url": url,
            "source": source,
            "verified_match": verified_match,
            "error": error
        }

