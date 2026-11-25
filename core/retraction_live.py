"""Live, per-paper retraction status for the single-drug analysis path.

A live run verifies a handful of papers and needs each one's *current* retraction
status, so it reads each paper directly from CrossRef by DOI
(`CrossRefClient.get_work_by_doi`) at run time. Any answer computed ahead of time
is only as current as the snapshot it came from.

`parse_retraction` is the pure core: given a CrossRef work record (the `message`
returned by get_work_by_doi, or None on a failed / 404 fetch) it returns the
tri-state retraction verdict. Direction matters: a paper's `updated-by[]` points
OUT to the notices that update it, and a retraction is an entry with
type == "retraction" whose `DOI` is the notice and whose `updated.date-parts` is
the date.
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import aiohttp

from core.references.clients import CrossRefClient

logger = logging.getLogger(__name__)

_RETRACTION_TYPE = "retraction"   # CrossRef `updated-by` entry type


def _format_date_parts(date_parts) -> str:
    """CrossRef `date-parts` ([[YYYY, M, D]]) -> 'YYYY' / 'YYYY-MM' / 'YYYY-MM-DD'
    at whatever precision is present; '' on malformed input."""
    try:
        parts = date_parts[0]
    except (TypeError, IndexError):
        return ""
    if not parts or parts[0] is None:
        return ""
    out = f"{int(parts[0]):04d}"
    if len(parts) > 1 and parts[1]:
        out += f"-{int(parts[1]):02d}"
        if len(parts) > 2 and parts[2]:
            out += f"-{int(parts[2]):02d}"
    return out


def _retraction_entries(updated_by) -> List[Dict[str, str]]:
    """Retraction notices from a work's `updated-by[]`, keeping only
    type == "retraction" (each entry's `DOI` is the notice)."""
    out = []
    for u in (updated_by or []):
        if (u.get("type") or "").lower() != _RETRACTION_TYPE:
            continue
        out.append({
            "notice_doi": u.get("DOI", "") or "",
            "date": _format_date_parts(u.get("updated", {}).get("date-parts")),
        })
    return out


def parse_retraction(message: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Tri-state retraction verdict for one CrossRef work record.

    `message` is what CrossRefClient.get_work_by_doi returns: the work record on a
    200, or None on a 404 / network / timeout / 5xx failure. Returns a dict ready
    to write onto a verified paper:

        message is None              -> retracted=None   (unknown / unchecked)
        updated-by has a retraction  -> retracted=True    (+ notice DOI, date)
        otherwise (fetched, clean)   -> retracted=False

    None never means "not retracted": a failed fetch stays unknown, never a
    confident False.
    """
    if not message:
        return {"retracted": None, "retraction_date": None, "retraction_notice_doi": None}
    entries = _retraction_entries(message.get("updated-by"))
    if not entries:
        return {"retracted": False, "retraction_date": None, "retraction_notice_doi": None}
    # One verdict per paper: when several notices exist, the earliest-dated wins.
    entry = min(entries, key=lambda e: e["date"] or "9999")
    return {
        "retracted": True,
        "retraction_date": entry["date"] or None,
        "retraction_notice_doi": entry["notice_doi"] or None,
    }


# extract_doi / _DOI_RE below — and the date/entry parse above — are small,
# self-contained DOI/parse helpers kept local to this module so core carries no
# cross-layer imports.
_DOI_RE = re.compile(r'(10\.\d{4,}/[^\s"\'<>]+)')


def extract_doi(url: str) -> Optional[str]:
    """Pull a DOI out of a stored URL/string and normalize it: strip doi: / doi.org
    prefixes, drop the query/fragment the greedy class swallowed and trailing
    punctuation."""
    s = (url or "").strip()
    for prefix in ("doi:", "https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    m = _DOI_RE.search(s)
    if not m:
        return None
    return re.split(r'[?#]', m.group(1))[0].rstrip('.,;)')


async def flag_retractions_live(papers: List[Dict[str, Any]]) -> None:
    """Set the tri-state retraction flag on each verified paper by reading its
    *current* status from CrossRef — one live per-DOI GET per paper.

    In-place contract: resolves `p["doi"]` from the verified-match DOI or, failing
    that, the URL, then writes `retracted` / `retraction_date` /
    `retraction_notice_doi` onto each paper.

    A paper with no resolvable DOI is set to retracted=None with no network call.
    A failed lookup (404 / network / timeout) likewise yields None — never a
    confident False — and never aborts the run: get_work_by_doi returns None
    rather than raising, and any unexpected error is caught per-paper. Requests go
    through CrossRefClient's module-global CROSSREF_GLOBAL_LIMITER, so request
    pacing and 429 backoff are shared with the reference checker.
    """
    for p in papers:
        p["doi"] = p.get("doi") or extract_doi(p.get("url"))

    # No-DOI papers are unknown, decided without a network call.
    for p in papers:
        if not p.get("doi"):
            p.update({"retracted": None, "retraction_date": None,
                      "retraction_notice_doi": None})

    to_check = [p for p in papers if p.get("doi")]
    if not to_check:
        return

    client = CrossRefClient()
    timeout = aiohttp.ClientTimeout(total=60, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # One /works?filter=doi:… request per ~100 DOIs instead of one GET per paper.
            # Behavior-equivalent: an absent DOI -> None (unknown), a failed chunk falls
            # back per-DOI, so parse_retraction sees exactly what the per-DOI path fed it.
            records = await client.get_works_by_dois([p["doi"] for p in to_check], session)
    except Exception as e:
        # The whole batch failing must never abort the run; leave papers unknown.
        logger.warning("Live retraction batch lookup failed: %r", e)
        records = {}

    for p in to_check:
        p.update(parse_retraction(records.get(p["doi"])))
