"""A 429 must lower the shared rate, not stall every concurrent run.

`backoff()` used to set a process-wide `_blocked_until`, so one unlucky request gated S2
for all 32 in-flight drug runs; on unblock they resumed at the old rate and tripped it
again. Measured over a 1,000-drug run: 1,734 backoffs, S2 gated 70% of wall clock,
median open window 1.4 s, effective ~0.22 RPS against a nominal 5.

Now: AIMD on the shared rate (halve on 429, creep up on success), no stall, plus jitter so
waiters released on the same deadline don't re-synchronise. A server-sent Retry-After is
still honoured literally.
"""
import time

import core.references.clients as clients


def _lim(rate=5.0, name="S2"):
    return clients.GlobalRateLimiter(requests_per_second=rate, name=name)


def test_429_halves_the_rate_and_does_not_gate():
    lim = _lim()
    lim.backoff(5.0)
    assert lim.rate == 2.5, "a 429 should multiplicatively decrease the shared rate"
    assert lim._blocked_until <= time.time(), "a 429 without Retry-After must NOT gate the fleet"


def test_repeated_429s_decay_to_a_floor_never_to_zero():
    lim = _lim()
    for _ in range(12):
        lim.backoff(5.0)
    assert lim.rate == lim.min_rate
    assert lim.min_rate > 0, "must keep making progress, not stop"
    assert lim._blocked_until <= time.time()


def test_sustained_success_probes_the_rate_back_up():
    lim = _lim()
    lim.backoff(5.0)
    dropped = lim.rate
    for _ in range(lim._probe_after * 3):
        lim.report_success()
    assert lim.rate > dropped, "successes should additively increase the rate"
    assert lim.rate <= lim.max_rate, "must never exceed the configured ceiling"


def test_retry_after_is_honoured_literally_and_leaves_the_rate_alone():
    lim = _lim(name="CrossRef")
    lim.backoff(12.0, server_asked=True)
    assert lim._blocked_until > time.time(), "an explicit Retry-After should gate"
    assert lim.rate == 5.0, "a server-named delay is not evidence our rate is wrong"


def test_acquire_actually_jitters_the_wait():
    """Without it, every waiter wakes on the same deadline and the herd re-forms.

    Asserting `lim.jitter > 0` is not enough — that passes even if acquire() never reads
    it. Nor is "the waits differ": token accrual makes them differ by microseconds anyway.
    The discriminator is the SPREAD. With the bucket drained at rate=1.0 the deterministic
    wait is ~1.0 s every time, so without jitter the spread is clock noise (~1e-5 s); with
    it the spread is a large fraction of `jitter / rate` = 0.5 s.
    """
    import asyncio

    lim = _lim(rate=1.0)
    waits = []

    async def _drain():
        real_sleep = asyncio.sleep

        async def _record(d):
            waits.append(d)
            await real_sleep(0)      # don't actually wait
            lim._tokens = 1.0        # release the waiter so acquire() returns

        asyncio.sleep = _record
        try:
            for _ in range(25):
                lim._tokens = 0.0
                lim._updated = time.time()
                await lim.acquire()
        finally:
            asyncio.sleep = real_sleep

    asyncio.run(_drain())
    assert waits, "acquire() never waited, so jitter could not be observed"
    spread = max(waits) - min(waits)
    assert spread > 0.05, (
        f"waits spanned only {spread:.6f}s across {len(waits)} samples — that is clock "
        "noise, not jitter. acquire() is not applying it, so every waiter released on one "
        "deadline wakes in lockstep."
    )


def test_jitter_is_bounded_by_one_interval():
    """Jitter must not become the dominant term — cap it at one request interval."""
    lim = _lim(rate=1.0)
    assert 0 < lim.jitter <= 1.0


def test_retry_after_gates_are_counted_separately():
    """A Retry-After gate is still a 429; it must show up in the telemetry, and be
    distinguishable from an AIMD decrease."""
    lim = _lim()
    lim.backoff(12.0, server_asked=True)
    assert lim._stat_429 == 1, "a gated 429 must still count toward 429s"
    assert lim._stat_gated == 1
    lim.backoff(5.0)
    assert lim._stat_429 == 2 and lim._stat_gated == 1, "an AIMD 429 must not count as gated"


def test_rate_telemetry_is_emitted_at_info(caplog):
    """A log of drops alone can't tell 'oscillating healthily' from 'pinned at the floor'.

    Recoveries happen silently every `_probe_after` successes, so the periodic snapshot is
    the only way to see the rate climbing back. It must be INFO — the batch logs at INFO
    and `probing up` is DEBUG, so without this the climbs are invisible.
    """
    import logging
    lim = _lim()
    lim.report_every = 0          # force a snapshot on the next event
    with caplog.at_level(logging.INFO, logger="core.references.clients"):
        lim.backoff(5.0)
    msgs = [r.message for r in caplog.records]
    snap = [m for m in msgs if "limiter: rate=" in m]
    assert snap, f"expected a rate snapshot at INFO, got {msgs}"
    assert "429s=1" in snap[-1] and "floor_hits=" in snap[-1]


def test_floor_hits_are_counted():
    lim = _lim()
    for _ in range(12):
        lim.backoff(5.0)
    assert lim.rate == lim.min_rate
    assert lim._stat_floor_hits > 0, "pinning at the floor must be visible in telemetry"


# --- the 429 handler itself -------------------------------------------------------------
# The limiter tests above cover backoff() in isolation. These cover the decision made in
# _make_request: what counts as "the server named a delay". Retry-After may legally be an
# HTTP-date, which float() cannot read; treating that parse failure as an instruction
# gated every in-flight run on a fabricated 10 s — the exact failure this change removes.

class _FakeResponse:
    def __init__(self, status, headers=None, payload=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Serves a scripted list of responses, one per request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None, params=None):
        self.calls += 1
        return self._responses.pop(0)


def _run_429_then_ok(retry_after_value, limiter):
    """One 429 carrying `retry_after_value`, then a 200. Returns the parsed body."""
    import asyncio

    client = clients.BaseClient()
    headers = {} if retry_after_value is None else {"Retry-After": retry_after_value}
    session = _FakeSession([
        _FakeResponse(429, headers),
        _FakeResponse(200, {}, {"ok": True}),
    ])
    data, status = asyncio.run(
        client._make_request(session, "https://api.example/x", limiter=limiter))
    assert status == 200 and data == {"ok": True}, "the retry should have succeeded"
    return session.calls


def test_http_date_retry_after_falls_through_to_aimd():
    """An unparseable Retry-After is not an instruction — we do not know what it said."""
    lim = _lim(rate=8.0)
    _run_429_then_ok("Wed, 21 Oct 2026 07:28:00 GMT", lim)
    assert lim._blocked_until <= time.time(), (
        "an unparseable Retry-After must NOT gate the fleet — we invented that number")
    assert lim.rate == 4.0, "it should take the AIMD path and halve the shared rate"
    assert lim._stat_gated == 0


def test_numeric_retry_after_is_obeyed_and_gates():
    """A value we actually read off the server IS an instruction."""
    lim = _lim(rate=8.0)
    _run_429_then_ok("0.05", lim)
    assert lim.rate == 8.0, "a server-named delay is not evidence our rate is wrong"
    assert lim._stat_gated == 1


def test_missing_retry_after_falls_through_to_aimd():
    """S2 never sends the header at all; that is the common path."""
    lim = _lim(rate=8.0)
    _run_429_then_ok(None, lim)
    assert lim._blocked_until <= time.time()
    assert lim.rate == 4.0
    assert lim._stat_gated == 0
