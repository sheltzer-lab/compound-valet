"""
Async API Clients for Reference Checker
"""

import aiohttp
import asyncio
import logging
import os
import random
import threading
import time
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import quote_plus, urlencode

from dotenv import load_dotenv

from core.references.settings import get_config
from core.references.utils import clean_title_for_search, normalize_text

load_dotenv()

logger = logging.getLogger(__name__)

def _env_num(name, default, cast=float):
    try:
        return cast(os.getenv(name) or default)
    except ValueError:
        return default


class GlobalRateLimiter:
    """
    Thread-safe global rate limiter for API requests.
    
    This rate limiter works correctly across:
    - Multiple coroutines in a single event loop (asyncio.gather)
    - Multiple threads, each with their own event loop (ThreadPoolExecutor + asyncio.run)
    
    Key design:
    - Uses threading.Lock (not asyncio.Lock) to protect shared state across threads
    - Calculates wait time under the lock, then releases lock before sleeping
    - This allows concurrent waiting while still guaranteeing proper request spacing
    """
    def __init__(self, requests_per_second: float = 1.0, name: str = "",
                 burst: Optional[float] = None):
        self.rate = float(requests_per_second)
        self.name = name
        # A token bucket: `rate` tokens accrue per second, capped at `capacity`, and each
        # request spends one. Banking up to `capacity` tokens during idle stretches lets a
        # following burst fire back-to-back and then settle to `rate`/sec — so we spend the
        # budget the polite pool gives us instead of forcing a fixed gap between requests.
        # Defaults to 1 token (≈ the old fixed pacer) for callers that don't opt in;
        # CrossRef passes a real burst.
        self.capacity = float(burst) if burst is not None else 1.0
        self._tokens = self.capacity
        self._updated = time.time()
        self._blocked_until = 0.0        # only a server-instructed Retry-After gates us now
        self.consecutive_errors = 0
        self._lock = threading.Lock()

        # AIMD (additive increase / multiplicative decrease) on the SHARED rate.
        # A 429 means our aggregate offered rate is too high — the fix is to offer less,
        # not to stall everyone. Halving on a 429 and creeping back up on sustained success
        # converges on whatever S2 actually grants, instead of oscillating between "blast
        # at max_rate" and "stop dead". `rate` is the live value; `max_rate` the ceiling.
        self.max_rate = self.rate
        self.min_rate = max(0.1, self.rate / 25.0)
        self.decrease_factor = 0.5
        self.increase_step = max(0.05, self.rate / 20.0)
        self._probe_after = 20           # successes before probing upward again
        self._successes = 0
        # Jitter: with tokens exhausted every waiter computes the SAME wait and they wake
        # in lockstep, so the retry storm re-synchronises after each penalty. Spread the
        # wake-ups over one inter-request interval.
        self.jitter = 0.5

        # Telemetry. The rate is the whole diagnosis for AIMD: a drop is logged on every
        # 429, but recoveries happen silently every `_probe_after` successes, so a log of
        # drops alone cannot distinguish "oscillating healthily around the true limit" from
        # "ratcheted to the floor and stuck". Emit a periodic snapshot at INFO instead.
        self._stat_429 = 0
        self._stat_ok = 0
        self._stat_floor_hits = 0   # 429s received while already pinned at min_rate
        self._stat_gated = 0        # 429s that carried a readable Retry-After and gated
        self._last_report = time.time()
        self.report_every = _env_num("LIMITER_REPORT_SECONDS", 120.0)

    def _maybe_report(self):
        """Call with the lock held."""
        now = time.time()
        if now - self._last_report < self.report_every:
            return
        self._last_report = now
        logger.info(f"{self.name} limiter: rate={self.rate:.2f}/{self.max_rate:.2f} RPS | "
                    f"429s={self._stat_429} ok={self._stat_ok} "
                    f"floor_hits={self._stat_floor_hits} gated={self._stat_gated}")

    async def acquire(self):
        """
        Acquire a rate-limited slot for making an API request.
        
        This method blocks (non-busy) until it's safe to make a request.
        Thread-safe: works across multiple threads with separate event loops.
        """
        while True:
            with self._lock:
                now = time.time()
                if now < self._blocked_until:
                    # Throttled by a recent 429 — wait it out; no tokens accrue meanwhile.
                    wait_time = self._blocked_until - now
                else:
                    # Refill for elapsed time (capped at capacity), then spend a token.
                    self._tokens = min(self.capacity,
                                       self._tokens + (now - self._updated) * self.rate)
                    self._updated = now
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return  # Got a token
                    wait_time = (1.0 - self._tokens) / self.rate
            
            # Wait OUTSIDE the lock so other requests can queue up
            # Only log if the wait is significant to avoid spam during normal rate limiting
            if wait_time > 1.0:
                logger.debug(f"{self.name}: Waiting {wait_time:.2f}s before next request")
            # de-synchronise waiters that woke on the same deadline
            if self.jitter and wait_time > 0:
                wait_time += random.uniform(0.0, self.jitter / max(self.rate, 0.1))
            await asyncio.sleep(wait_time)
            # Loop back and try again (another thread might have taken the slot)

    def report_success(self):
        """Reset the error count and, after a run of successes, probe the rate upward."""
        with self._lock:
            self.consecutive_errors = 0
            self._stat_ok += 1
            # We leave _blocked_until alone: any active backoff window drains on its own.
            if self.rate < self.max_rate:
                self._successes += 1
                if self._successes >= self._probe_after:
                    self._successes = 0
                    before = self.rate
                    self.rate = min(self.max_rate, self.rate + self.increase_step)
                    logger.debug(f"{self.name}: probing up {before:.2f} -> {self.rate:.2f} RPS")
            self._maybe_report()      # after the probe, so the snapshot is the current rate

    def backoff(self, delay: float, server_asked: bool = False):
        """Respond to a 429.

        ``server_asked`` — the response carried Retry-After, i.e. the server named a time.
        Honour it literally: gate until then. (S2 never sends one; CrossRef sometimes does.)

        Otherwise the 429 only tells us our *aggregate* rate is too high. Gating the whole
        process is the wrong lever — it stalls 32 in-flight runs for one unlucky request,
        and on unblock they all resume at the old rate and trip it again. Measured on a
        1,000-drug run (September 2026): 1,734 backoffs, S2 gated 14.15 h of 20.2 h = 70% of wall clock,
        median open window 1.4 s, effective 0.22 RPS against a nominal 5. So instead:
        halve the shared rate and keep going. Successes creep it back up.
        """
        with self._lock:
            self.consecutive_errors += 1
            self._successes = 0
            self._stat_429 += 1
            if server_asked and delay > 0:
                self._stat_gated += 1
                self._blocked_until = max(self._blocked_until, time.time() + delay)
                self._tokens = 0.0
                self._updated = self._blocked_until
                logger.warning(f"{self.name}: Retry-After {delay:.1f}s — gating until "
                               f"{self._blocked_until:.1f}")
                return
            old = self.rate
            self.rate = max(self.min_rate, self.rate * self.decrease_factor)
            if self.rate <= self.min_rate:
                self._stat_floor_hits += 1
            self._tokens = 0.0           # spend the bank; re-accrue at the new rate
            self._updated = time.time()
            logger.warning(f"{self.name}: 429 — rate {old:.2f} -> {self.rate:.2f} RPS "
                           f"(consecutive {self.consecutive_errors}, no fleet stall)")
            self._maybe_report()

class GlobalConcurrencyLimiter:
    """Process-wide cap on the number of requests in flight at once.

    An asyncio.Semaphore binds to the loop that created it, but this codebase runs
    asyncio.run() in several threads (one per drug), so a per-loop semaphore caps
    concurrency *per drug* — limit x N drugs — not globally. This counts in-flight
    requests under a threading.Lock so the cap holds across every loop in the process,
    and waits with asyncio.sleep outside the lock (same shape as GlobalRateLimiter).
    Use as an async context manager: ``async with CROSSREF_CONCURRENCY_LIMITER: ...``.
    """
    def __init__(self, limit: int, name: str = "", poll: float = 0.01):
        self.limit = max(1, int(limit))
        self.name = name
        self._poll = poll                 # how often to re-check when the gate is full
        self._in_flight = 0
        self._lock = threading.Lock()

    async def acquire(self):
        while True:
            with self._lock:
                if self._in_flight < self.limit:
                    self._in_flight += 1
                    return
            await asyncio.sleep(self._poll)

    def release(self):
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.release()
        return False


# These limiters are global *within one process*. When several processes run the
# pipeline at once (for example, one subprocess per drug and run), each gets its own limiter, so N workers hit the API at N x the intended rate. That
# is what makes CrossRef 429 in batch runs. If you run N pipeline processes at once, set
# API_RATE_DIVISOR=N; each then takes that fraction of the budget, so the aggregate
# across them is the rate named below. Unset means a
# divisor of 1 and unchanged behaviour.
try:
    _rate_divisor = max(1.0, float(os.getenv("API_RATE_DIVISOR") or 1))
except ValueError:
    _rate_divisor = 1.0

# Semantic Scholar rate limits:
# - Without API key: 1 RPS (strict public limit)
# - With API key: Higher limit (we use 5 RPS to be conservative)
# NOT divided: measured over a 125-unit batch at 8 workers, S2 produced 66 rate-limit
# events against CrossRef's 1,471, i.e. it copes with the fan-out. Dividing it 8-fold
# would serialise ingestion (one request per 8s per unit) and cost more time than the
# 429s do. Revisit if S2 events climb.
_s2_has_api_key = bool(os.getenv("SEMANTIC_SCHOLAR_API_KEY"))
_s2_rps = 5.0 if _s2_has_api_key else 1.0
S2_GLOBAL_LIMITER = GlobalRateLimiter(requests_per_second=_s2_rps, name="S2")

# --- 429 handling ----------------------------------------------------------------------
# A 429 says our AGGREGATE offered rate is too high. It used to set a process-wide gate:
# one unlucky request stopped S2 for every concurrent drug run, and on unblock all 32
# threads resumed at the old rate and tripped it again. Measured over a 1,000-drug run
# (September 2026): 1,734 backoffs, 1,659 blocks, S2 gated 14.15 h of a 20.2 h span = 70%
# of wall clock; median open window 1.4 s (~7 requests at 5 RPS before the next 429), so
# effective throughput was ~0.22 RPS against a nominal 5 — plausibly worse than a plain
# 1 RPS key would have given.
#
# GlobalRateLimiter now answers a 429 with AIMD on the shared rate (halve; creep back up
# on sustained success) and no fleet stall, plus jitter so waiters don't re-synchronise.
# A server-sent Retry-After is still honoured literally — S2 never sends one, CrossRef does.
#   S2_BACKOFF_FALLBACK=5   delay recorded when there is no Retry-After
S2_BACKOFF_FALLBACK = _env_num("S2_BACKOFF_FALLBACK", 5.0)

logger.info(f"Semantic Scholar rate limiter: {_s2_rps} RPS (API key: {_s2_has_api_key}) | "
            f"429 -> AIMD on the shared rate, no fleet stall")

# CrossRef polite pool, from CrossRef's own response headers (2026-08-15) with our mailto:
#   polite pool   x-api-pool: polite-single   10 req/s   x-concurrency-limit: 3
#   anonymous     x-api-pool: public-single    5 req/s   x-concurrency-limit: 1
# Both axes must be respected: 10 req/s AND at most 3 in flight. simple_batch runs every
# drug as a thread in ONE process, so a single shared rate limiter + a single shared
# concurrency gate already meter the whole batch: when drugs fall out of step, whichever
# are fetching references right then share the full budget between them. (The
# API_RATE_DIVISOR path above only matters when each drug runs as its own process,
# which cannot share these in-process objects.)
# A Metadata Plus token moves us again. Measured 2026-09-11 with our own token:
#   plus-single   x-api-pool: plus-single   150 req/s   (no x-concurrency-limit sent)
# Take the rate: it is CrossRef's own number, same as the two above. Do NOT read the
# concurrency the same way — the plus pool advertises none, so 25 is a chosen value, not
# a measured one. It is high enough that the rate limiter stays the binding constraint
# (25 in flight at CrossRef's typical sub-second response is ~an order of 100/s, which
# the 150/s bucket then paces) rather than the gate throttling us below what we pay for.
# Nothing here bounds it for us if that estimate is wrong: move it off a real batch's
# 429 count, not off this comment.
#
# Read at import: a load_dotenv() that runs later leaves us on the polite budget, which
# is the safe direction to be wrong in.
def _env_budget(name: str, default, cast=float):
    """A numeric override that cannot take the process down.

    A bad value here is a typo in a setting, not a reason for the pipeline
    to die — and the failures are not the loud kind. The token bucket divides by its rate,
    so `0` raises ZeroDivisionError on the first CrossRef request (inside the pipeline, not
    at boot) and a negative rate makes `acquire()` spin forever without ever granting a
    token; a non-numeric value is the only one that fails at import. Refuse all three
    loudly and keep the default.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number - ignoring it, using %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%r must be greater than zero - ignoring it, using %s", name, raw, default)
        return default
    return value


_crossref_has_plus = bool(os.getenv("CROSSREF_API_TOKEN"))
CROSSREF_RPS_BUDGET = _env_budget("CROSSREF_RPS_BUDGET",
                                  150.0 if _crossref_has_plus else 10.0)
CROSSREF_CONCURRENCY_BUDGET = _env_budget("CROSSREF_CONCURRENCY_BUDGET",
                                          25 if _crossref_has_plus else 3, cast=int)

# Target the pool's documented budget exactly — a rare boundary 429 is handled by
# backoff() + Retry-After (and the polite pool warns before it throttles), which is a
# measured response, not a reason to leave budget unused.
_crossref_rps = CROSSREF_RPS_BUDGET / _rate_divisor
# burst=~1 second of allowance: enough to absorb a desynced burst, small enough to stay
# inside the pool's window.
CROSSREF_GLOBAL_LIMITER = GlobalRateLimiter(requests_per_second=_crossref_rps,
                                            name="CrossRef", burst=_crossref_rps)
CROSSREF_MAX_CONCURRENCY = max(1, int(CROSSREF_CONCURRENCY_BUDGET // _rate_divisor))
# ONE gate for the whole process, shared across every drug thread and its own event loop.
CROSSREF_CONCURRENCY_LIMITER = GlobalConcurrencyLimiter(CROSSREF_MAX_CONCURRENCY, name="CrossRef")
# Logged unconditionally: whether the token was picked up is the first thing you want from
# the log when CrossRef starts 429ing, and API_RATE_DIVISOR silently rescales both axes.
logger.info(f"CrossRef rate limiter: {_crossref_rps:g} RPS / {CROSSREF_MAX_CONCURRENCY} concurrent "
            f"(Metadata Plus: {_crossref_has_plus}, API_RATE_DIVISOR: {_rate_divisor:g})")

# CrossRef routes requests that identify a contact address to its "polite pool",
# which gets better and more consistent service than the anonymous pool (and
# CrossRef mails you before throttling instead of just 429ing). Identification is
# simply a `mailto:` in the User-Agent. Every call site constructs CrossRefClient()
# with no argument, so the address defaults here rather than at every call site.
CROSSREF_MAILTO = os.getenv("CROSSREF_MAILTO")

# --- Semantic Scholar rate-limit instrumentation ----------------------------------
# Read-only capture of S2's OWN rate-limit response headers, so a real batch reveals the
# actual granted rate for our (default-tier) key — the 5 RPS above is an assumption, not
# measured. Never changes request behaviour. Deduped to avoid per-request spam: the full
# header set once (for discovery), then each endpoint's first 200 and every 429. Grep the
# batch logs for `S2_RATELIMIT`.
_S2_RL_LOCK = threading.Lock()
_S2_RL_SEEN: set = set()
_S2_RL_DUMPED = False


def _s2_endpoint(url: str) -> str:
    low = url.lower()
    if "/search/bulk" in low:
        return "paper/search/bulk"
    if "/paper/search" in low:
        return "paper/search"
    if "/paper/" in low:
        return "paper/get"
    return url.split("?", 1)[0]


def _capture_s2_ratelimit(status: int, url: str, headers) -> None:
    """Log Semantic Scholar's rate-limit headers. Must never raise into a request."""
    global _S2_RL_DUMPED
    try:
        ep = _s2_endpoint(url)
        rl = {k: v for k, v in headers.items()
              if any(s in k.lower() for s in ("ratelimit", "rate-limit", "retry-after", "throttl"))}
        with _S2_RL_LOCK:
            first = not _S2_RL_DUMPED
            _S2_RL_DUMPED = True
            key = (ep, status)
            new_pair = key not in _S2_RL_SEEN
            _S2_RL_SEEN.add(key)
        if first:
            safe = {k: v for k, v in headers.items()
                    if k.lower() not in ("set-cookie", "authorization")}
            logger.warning(f"S2_RATELIMIT discovery endpoint={ep} status={status} all_headers={safe}")
        if status == 429 or new_pair:
            logger.warning(f"S2_RATELIMIT endpoint={ep} status={status} ratelimit_headers={rl}")
    except Exception:
        pass


class BaseClient:
    def __init__(self):
        self.config = get_config()
        self.headers = {"User-Agent": "CompoundVALET/1.0"}

    async def _make_request(
        self, 
        session: aiohttp.ClientSession, 
        url: str, 
        params: Optional[Dict] = None, 
        limiter: Optional[GlobalRateLimiter] = None,
        method: str = "GET"
    ) -> Tuple[Optional[Any], int]:
        """
        Execute API request with robust error handling, rate limiting, and retries.
        """
        retries = 3
        current_attempt = 0
        
        while current_attempt <= retries:
            if limiter:
                await limiter.acquire()
                
            try:
                if method == "GET":
                    response_ctx = session.get(url, headers=self.headers, params=params)
                else:
                    response_ctx = session.request(method, url, headers=self.headers, params=params)
                    
                async with response_ctx as response:
                    status = response.status
                    if limiter is not None and limiter.name == "S2":
                        _capture_s2_ratelimit(status, url, response.headers)
                    
                    if status == 200:
                        if limiter:
                            limiter.report_success()
                        try:
                            # Some APIs return non-JSON for certain errors or endpoints?
                            # We assume JSON for now as these are JSON APIs
                            data = await response.json()
                            return data, 200
                        except Exception as e:
                            logger.error(f"Failed to parse JSON from {url}: {e}")
                            return None, status
                            
                    elif status == 429:
                        # Rate Limit Hit
                        retry_after = response.headers.get("Retry-After")
                        delay = 0.0
                        parsed_retry_after = False
                        if retry_after:
                            try:
                                delay = float(retry_after)
                                parsed_retry_after = delay > 0
                            except ValueError:
                                # Retry-After may legally be an HTTP-date. We do not parse
                                # that, so we do NOT know what the server asked for — fall
                                # through to AIMD rather than gating every in-flight run on
                                # a number we invented.
                                logger.debug(f"Unparseable Retry-After {retry_after!r} at {url}"
                                             " — treating as no header")
                                delay = 0.0

                        # Only a value we actually read off the server counts as instruction.
                        is_s2 = bool(limiter and limiter.name == "S2")
                        server_asked = parsed_retry_after
                        if not delay:
                            delay = S2_BACKOFF_FALLBACK if is_s2 else 5.0

                        logger.warning(f"Rate limited (429) at {url} "
                                       f"(attempt {current_attempt + 1}/{retries + 1})")
                        if limiter:
                            # AIMD: halves the SHARED rate, no fleet stall (see backoff()).
                            limiter.backoff(delay, server_asked=server_asked)
                        else:
                            await asyncio.sleep(delay + random.uniform(0, delay * 0.25))

                        current_attempt += 1
                        continue # Retry loop
                        
                    elif status >= 500:
                        # Server Error - simple retry
                        logger.warning(f"Server error {status} at {url}. Retrying...")
                        await asyncio.sleep(1.0 * (2 ** current_attempt))
                        current_attempt += 1
                        continue
                        
                    else:
                        # Other Client Error (404, 403, etc) - Do not retry
                        # Read body for debugging?
                        # text = await response.text()
                        # logger.debug(f"Error {status} body: {text[:200]}")
                        return None, status
                        
            except Exception as e:
                logger.error(f"Request exception for {url}: {e}")
                # Network error? Retry
                await asyncio.sleep(1.0)
                current_attempt += 1
                
        logger.error(f"Max retries exceeded for {url}")
        return None, 0

class SemanticScholarClient(BaseClient):
    def __init__(self):
        super().__init__()
        self.base_url = self.config["semantic_scholar"]["base_url"]
        api_key = self.config["semantic_scholar"].get("api_key")
        if api_key:
            self.headers["x-api-key"] = api_key

    async def search_paper_bulk(self, titles: List[str], session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """
        Search for multiple papers on Semantic Scholar using bulk API.
        Uses OR logic combined with AND logic for better precision.
        """
        if not titles:
            return []
            
        # Semantic Scholar bulk search endpoint supports boolean logic
        # We process in chunks to avoid URL length issues or complexity limits
        CHUNK_SIZE = 10  # Conservative chunk size for complex queries
        all_results = []
        
        for i in range(0, len(titles), CHUNK_SIZE):
            chunk = titles[i:i + CHUNK_SIZE]
            
            # Construct query: (title1) | (title2) | ...
            # Clean titles and wrap in parens
            cleaned_titles = [clean_title_for_search(t) for t in chunk]
            valid_titles = [t for t in cleaned_titles if t]
            
            if not valid_titles:
                continue
                
            # Build the OR query
            or_clauses = [f"({t})" for t in valid_titles]
            query = "|".join(or_clauses)
            
            url = f"{self.base_url}/paper/search/bulk"
            params = {
                "query": query,
                "fields": "title,authors,year,externalIds,url,isOpenAccess,openAccessPdf,publicationTypes,journal,publicationVenue",
                "limit": 1000  # S2 Bulk API limit per page
            }
            
            data, status = await self._make_request(session, url, params=params, limiter=S2_GLOBAL_LIMITER)
            
            if status == 200 and data:
                results = data.get('data', [])
                all_results.extend(results)
            else:
                logger.warning(f"Semantic Scholar bulk search failed: {status}")
                
        return all_results


class CrossRefClient(BaseClient):
    """CrossRef API client - excellent for DOI verification and publisher-registered content"""
    def __init__(self, email: Optional[str] = None):
        super().__init__()
        self.base_url = "https://api.crossref.org"
        # Polite pool by default; pass email="" to opt back into the anonymous pool.
        email = CROSSREF_MAILTO if email is None else email
        ua = "CompoundVALET/1.0"
        ua += f" (mailto:{email})" if email else ""
        self.headers = {"Accept": "application/json", "User-Agent": ua}
        # Metadata Plus (paid): the token moves us to the `plus-single` pool. Header name
        # and "Bearer " prefix are CrossRef's, not ours. Read per-instance so a late
        # load_dotenv() still takes effect.
        token = os.getenv("CROSSREF_API_TOKEN")
        if token:
            self.headers["Crossref-Plus-API-Token"] = f"Bearer {token}"

    async def _make_request(self, *args, **kwargs):
        """Cap in-flight CrossRef requests at the pool's concurrency limit — process-wide.

        Callers gather over every paper at once, so without this the rate limiter paces
        requests but nothing stops 30+ being open simultaneously. The gate is shared
        across every drug's event loop (CROSSREF_CONCURRENCY_LIMITER), and is held across
        the inherited retry loop on purpose: a retrying request is still one in flight.
        """
        async with CROSSREF_CONCURRENCY_LIMITER:
            return await super()._make_request(*args, **kwargs)

    async def search_works(self, query: str, session: aiohttp.ClientSession, year: Optional[int] = None, type_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search for works matching the query.

        Args:
            type_filter: Override the default type filter string. E.g. "type:journal-article"
                         to restrict results to journal articles only.
        """
        url = f"{self.base_url}/works"
        params = {
            "query": query,
            "rows": 5,
            "select": "DOI,title,author,published,publisher,container-title,type,URL,link"
        }
        # Filter to actual papers only (excludes supplementary materials, datasets, etc. which lack author/year)
        # Accepted types: journal-article, proceedings-article, book-chapter, posted-content (preprints)
        if type_filter:
            filters = [type_filter]
        else:
            filters = ["type:journal-article,type:proceedings-article,type:book-chapter,type:posted-content"]
        if year:
            filters.append(f"from-pub-date:{year}")
            filters.append(f"until-pub-date:{year}")
        params["filter"] = ",".join(filters)
        
        data, status = await self._make_request(session, url, params=params, limiter=CROSSREF_GLOBAL_LIMITER)
        
        if status == 200 and data:
            return data.get('message', {}).get('items', [])
        return []

    @staticmethod
    def _clean_doi(doi: str) -> str:
        """Strip doi:/https://doi.org/ prefixes to the bare DOI CrossRef expects."""
        clean_doi = doi or ""
        if clean_doi.startswith('doi:'):
            clean_doi = clean_doi[4:]
        if clean_doi.startswith('https://doi.org/'):
            clean_doi = clean_doi[len('https://doi.org/'):]
        if clean_doi.startswith('http://doi.org/'):
            clean_doi = clean_doi[len('http://doi.org/'):]
        return clean_doi

    async def get_work_by_doi(self, doi: str, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Get work data by DOI (one per-DOI request)."""
        url = f"{self.base_url}/works/{self._clean_doi(doi)}"
        data, status = await self._make_request(session, url, limiter=CROSSREF_GLOBAL_LIMITER)
        if status == 200 and data:
            return data.get('message')
        return None

    async def get_works_by_dois(self, dois: List[str], session: aiohttp.ClientSession,
                                chunk_size: int = 100) -> Dict[str, Optional[Dict[str, Any]]]:
        """Batch DOI lookup via ``/works?filter=doi:…`` — behavior-equivalent to calling
        get_work_by_doi per DOI, but one request per ~chunk_size DOIs.

        Returns ``{original_doi: work-record or None}``. A DOI absent from a *successful*
        chunk maps to None — identical to a per-DOI 404. A whole chunk whose request fails
        falls back to per-DOI, so a transient error never silently marks papers unknown.

        chunk_size is capped so the GET stays under CrossRef's ~8 KB request-URI limit
        (measured: 414 above ~250 DOIs); ``rows == chunk`` so a full chunk returns in one
        page (CrossRef's default page size is 20).
        """
        uniq, seen = [], set()
        for d in dois:
            cd = self._clean_doi(d)
            if cd and cd.lower() not in seen:
                seen.add(cd.lower())
                uniq.append(cd)

        record_by_key: Dict[str, Optional[Dict[str, Any]]] = {}
        for i in range(0, len(uniq), chunk_size):
            chunk = uniq[i:i + chunk_size]
            params = {"filter": ",".join(f"doi:{d}" for d in chunk),
                      "select": "DOI,updated-by", "rows": len(chunk)}
            url = f"{self.base_url}/works?{urlencode(params)}"
            data, status = await self._make_request(session, url, limiter=CROSSREF_GLOBAL_LIMITER)
            if status == 200 and data:
                items = (data.get("message") or {}).get("items") or []
                by_doi = {(it.get("DOI") or "").lower(): it for it in items}
                for d in chunk:
                    record_by_key[d.lower()] = by_doi.get(d.lower())   # absent -> None (== per-DOI 404)
            else:
                for d in chunk:                                        # chunk failed: per-DOI fallback
                    record_by_key[d.lower()] = await self.get_work_by_doi(d, session)

        return {d: record_by_key.get(self._clean_doi(d).lower()) for d in dois}

