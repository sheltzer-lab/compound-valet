"""Running pipeline work across threads.

ContextThreadPoolExecutor — a ThreadPoolExecutor that carries contextvars across
the thread boundary. Used by the analysis pipeline wherever it runs (simple_batch.py).

Stdlib-only by design, so any module can import it without an import cycle.
"""
import concurrent.futures
import contextvars


class ContextThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor that runs each task inside a COPY of the submitting
    thread's contextvars.Context.

    ContextVars don't cross thread boundaries on their own (PEP 567), so ambient
    per-run state -- such as a consensus run's scratch database (core.scratch_db) -- would revert to defaults inside
    worker threads. Copying the context at submit time carries all of it,
    uniformly, with no per-variable wiring; each task gets its own copy, so
    nothing leaks between tasks that reuse the same pool thread.
    """

    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)
