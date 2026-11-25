"""Offline PubChem lookups over a prebuilt SQLite index.

The index is built by ``scripts/build_pubchem_index.py`` from PubChem's bulk
flat files (CID-Synonym, CID-Parent, CID-SMILES). PubChem's live API is
unreliable (503s / rate limits / blocks), so the resolver never
touches the network at request time — it reads only this static index.

All lookups are indexed (microsecond) and safe for concurrent readers: the live
pipeline runs ~10-20 resolutions at once (``simple_batch.py --workers 10``), so
each thread gets its own read-only connection.

Index schema (see the builder):
    syn(cid INTEGER, syn TEXT, syn_norm TEXT)          INDEX(syn_norm), INDEX(cid)
    cid_parent(cid INTEGER PRIMARY KEY, parent_cid INTEGER)   INDEX(parent_cid)
    cid_smiles(cid INTEGER PRIMARY KEY, smiles TEXT)

``syn_norm`` is ``core.db.normalize_key(synonym)`` so a query normalizes the
same way the index was keyed (e.g. "BAY 43-9006", "BAY43-9006" and "bay 439006"
all collapse to the same key).
"""

import os
import sqlite3
import threading

from dotenv import load_dotenv

from core.db import normalize_key

load_dotenv()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INDEX = os.path.join(_REPO_ROOT, "data", "pubchem", "pubchem_offline.sqlite")


class PubChemOffline:
    """Read-only accessor for the offline PubChem index."""

    def __init__(self, db_path=None):
        self.db_path = db_path or os.getenv("PUBCHEM_INDEX") or DEFAULT_INDEX
        self._local = threading.local()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not os.path.exists(self.db_path):
                raise FileNotFoundError(
                    f"PubChem index not found at {self.db_path}. "
                    "Build it with scripts/build_pubchem_index.py (--mini for tests, "
                    "--full for the real index)."
                )
            # One read-only connection per thread (concurrent-safe).
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
            )
            self._local.conn = conn
        return conn

    # ---- name -> candidate CIDs ------------------------------------------
    def candidates_for_name(self, name):
        """All CIDs that carry ``name`` (normalized) as a synonym. May be empty."""
        key = normalize_key(name)
        if not key:
            return []
        rows = self._conn().execute(
            "SELECT DISTINCT cid FROM syn WHERE syn_norm = ?", (key,)
        ).fetchall()
        return [r[0] for r in rows]

    # ---- CID -> synonyms --------------------------------------------------
    def synonyms_for_cid(self, cid):
        rows = self._conn().execute(
            "SELECT syn FROM syn WHERE cid = ?", (int(cid),)
        ).fetchall()
        return [r[0] for r in rows]

    def synonyms_for_cids(self, cids):
        """Union of synonyms across ``cids`` (order-stable, deduped)."""
        ids = list({int(c) for c in cids})
        if not ids:
            return []
        conn = self._conn()
        seen, out = set(), []
        for i in range(0, len(ids), 500):  # SQLite has a host-parameter cap
            chunk = ids[i:i + 500]
            q = "SELECT syn FROM syn WHERE cid IN (%s)" % ",".join("?" * len(chunk))
            for (s,) in conn.execute(q, chunk):
                if s not in seen:
                    seen.add(s)
                    out.append(s)
        return out

    # ---- CID -> parent / salt union --------------------------------------
    def parent_cid(self, cid):
        """Desalted parent CID. Falls back to ``cid`` if no parent row exists."""
        row = self._conn().execute(
            "SELECT parent_cid FROM cid_parent WHERE cid = ?", (int(cid),)
        ).fetchone()
        return row[0] if row else int(cid)

    def salt_union_cids(self, cid, limit=500):
        """``cid`` + its parent + every CID sharing that parent (salts, hydrates,
        protonation states). The set of forms that are the *same molecule*.

        ``limit`` caps the sibling count: a small common moiety (e.g. glycine) is the
        parent of thousands of derivatives, and pulling them all in would explode the
        synonym set with unrelated names — so we bound it."""
        parent = self.parent_cid(cid)
        rows = self._conn().execute(
            "SELECT cid FROM cid_parent WHERE parent_cid = ? LIMIT ?", (int(parent), limit)
        ).fetchall()
        return {int(cid), int(parent)} | {r[0] for r in rows}

    def salt_union_synonyms(self, cid):
        """Every transitive synonym reachable from ``cid`` via the salt union."""
        return self.synonyms_for_cids(self.salt_union_cids(cid))

    # ---- CID -> structure -------------------------------------------------
    def smiles_for_cid(self, cid):
        row = self._conn().execute(
            "SELECT smiles FROM cid_smiles WHERE cid = ?", (int(cid),)
        ).fetchone()
        return row[0] if row else None


_default = None


def get_index():
    """Process-wide default index (reads ``$PUBCHEM_INDEX`` or the default path)."""
    global _default
    if _default is None:
        _default = PubChemOffline()
    return _default
