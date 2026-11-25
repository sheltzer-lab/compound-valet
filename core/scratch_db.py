"""Per-run scratch DB routing.

The consensus runner isolates each pipeline run's DB scratch (verified papers,
identity — everything) into a throwaway local SQLite, so parallel runs of the same
drug don't collide and the target DB only ever receives the winner.

The active URL lives in a contextvar, so setting it reaches the pipeline's
ContextThreadPoolExecutor workers and asyncio tasks. Unset — the default, and every
path outside the consensus runner — means ``core.db.init_db`` builds the global
target engine exactly as it always has.

This sits beside ``core.db`` rather than inside it: only the four-line override in
``init_db`` belongs to the shared DB layer; the engine cache and the context manager
are consensus machinery. ``core.db`` is imported lazily below so the two modules
don't form an import cycle.
"""
import contextlib
import contextvars
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_scratch_url = contextvars.ContextVar("scratch_db_url", default=None)
_engines = {}                  # url -> (engine, sessionmaker)
_lock = threading.Lock()


def current() -> str | None:
    """The scratch URL in force for this context, or None for the normal engine."""
    return _scratch_url.get()


def sessionmaker_for(url: str):
    """Sessionmaker bound to the scratch SQLite at `url`, created on first use."""
    from core.db import Base          # deferred: core.db imports this module
    with _lock:
        entry = _engines.get(url)
        if entry is None:
            engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
            Base.metadata.create_all(engine)
            entry = (engine, sessionmaker(bind=engine))
            _engines[url] = entry
        return entry[1]


@contextlib.contextmanager
def scratch_db(url: str):
    """Route all DB access in this context — and its ContextThreadPoolExecutor workers
    and asyncio tasks — to an isolated scratch SQLite at `url`. Restores on exit."""
    token = _scratch_url.set(url)
    try:
        yield
    finally:
        _scratch_url.reset(token)


def dispose_scratch_db(url: str):
    """Drop a scratch engine and close its connections (call after capturing the run)."""
    with _lock:
        entry = _engines.pop(url, None)
    if entry:
        entry[0].dispose()
