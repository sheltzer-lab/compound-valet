#!/usr/bin/env python
"""Build the offline PubChem index (SQLite) from PubChem bulk flat files.

Flat files (each ``CID<TAB>value``, sorted by CID ascending):
  CID-Synonym-filtered.gz / CID-Synonym-unfiltered.gz   CID -> synonym (many/CID)
  CID-Parent.gz                                          CID -> parent (desalted) CID
  CID-SMILES.gz                                          CID -> isomeric SMILES

The live resolver reads the result via ``core/pubchem_offline.py``, so this is the
only PubChem touch and it happens offline.

Modes
  --mini --names "C1632,Imatinib,Gleevec,..."   tiny index over just those drugs
        (+ their salt-union siblings). ~minutes. For unit tests / fast iteration.
  --full                                         the real index (~30-90 min; run
        in the background). cid_smiles/cid_parent are restricted to "named"
        compounds (CIDs that appear in CID-Synonym) + their parents.
  --calibrate [--sample-cids N]                  build over the first N CIDs only
        to estimate full size / timing before committing to the full run.

Source dir: --src, else $PUBCHEM_FLAT_DIR, else data/pubchem/flat.
Output:     --out, else data/pubchem/pubchem_offline[_mini].sqlite
Synonyms:   --unfiltered uses the larger unfiltered file (better recall on
            obscure research codes; the LLM filter cleans the extra junk).
"""

import argparse
import io
import os
import re
import sqlite3
import subprocess
import sys
import time

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True) or None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import normalize_key  # noqa: E402

DEFAULT_SRC = "data/pubchem/flat"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def fast_norm(s):
    """Same output as core.db.normalize_key, but skips the Greek-transliteration loop
    for ASCII strings (~all synonyms) — ~10x faster over 150M+ rows. Non-ASCII falls
    back to normalize_key so keys still match the reader."""
    if s.isascii():
        return _NON_ALNUM.sub("", s.lower())
    return normalize_key(s)


def _flat(src, base, unfiltered=False):
    if base == "synonym":
        name = "CID-Synonym-unfiltered.gz" if unfiltered else "CID-Synonym-filtered.gz"
    else:
        name = {"parent": "CID-Parent.gz", "smiles": "CID-SMILES.gz"}[base]
    path = os.path.join(src, name)
    if not os.path.exists(path):
        sys.exit(f"missing flat file: {path}")
    return path


def _parse(line):
    cid_s, tab, val = line.rstrip("\n").partition("\t")
    if not tab or not val:
        return None
    try:
        return int(cid_s), val
    except ValueError:
        return None


def stream(path, limit=None):
    """Yield (cid:int, value:str) from a gzipped ``CID<TAB>value`` flat file.

    Uses the system ``gzip -dc`` (fast C decompression) rather than Python's gzip.
    """
    proc = subprocess.Popen(["gzip", "-dc", path], stdout=subprocess.PIPE)
    n = 0
    try:
        for line in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace"):
            row = _parse(line)
            if row is None:
                continue
            yield row
            n += 1
            if limit and n >= limit:
                break
    finally:
        proc.stdout.close()
        proc.terminate()
        proc.wait()


def grep_stream(path, pattern, ignore_case=False):
    """``gzip -dc path | grep -E pattern`` — prefilter in C so Python only sees
    matching lines (mini builds: ~1 min instead of normalizing 150M+ rows)."""
    p1 = subprocess.Popen(["gzip", "-dc", path], stdout=subprocess.PIPE)
    flags = "-iE" if ignore_case else "-E"
    p2 = subprocess.Popen(["grep", flags, pattern], stdin=p1.stdout, stdout=subprocess.PIPE)
    p1.stdout.close()
    try:
        for line in io.TextIOWrapper(p2.stdout, encoding="utf-8", errors="replace"):
            row = _parse(line)
            if row is not None:
                yield row
    finally:
        p2.stdout.close()
        p2.wait()
        p1.wait()


TAB = "\t"  # a real tab — grep -E on macOS treats "\t" in a pattern as literal 't'


def grep_fixed_stream(path, terms):
    """``gzip -dc path | grep -iF -e term ...`` — fast fixed-string (substring,
    case-insensitive) prefilter. A complex ``-iE`` regex over the 7.5 GB stream is
    pathologically slow on BSD grep; fixed strings are not."""
    p1 = subprocess.Popen(["gzip", "-dc", path], stdout=subprocess.PIPE)
    args = ["grep", "-iF"]
    for t in terms:
        args += ["-e", t]
    p2 = subprocess.Popen(args, stdin=p1.stdout, stdout=subprocess.PIPE)
    p1.stdout.close()
    try:
        for line in io.TextIOWrapper(p2.stdout, encoding="utf-8", errors="replace"):
            row = _parse(line)
            if row is not None:
                yield row
    finally:
        p2.stdout.close()
        p2.wait()
        p1.wait()


def _name_terms(names):
    """Fixed-string prefilter terms for a set of drug names: each raw name plus its
    de-punctuated form (so 'BAY 43-9006' also matches a stored 'BAY439006')."""
    terms = set()
    for n in names:
        n = n.strip()
        if n:
            terms.add(n)
            terms.add(re.sub(r"[^A-Za-z0-9]", "", n))
    return [t for t in terms if t]


def _cid_head_pattern(cids):
    return r"^(" + "|".join(str(c) for c in cids) + r")" + TAB


def _cid_tail_pattern(cids):
    return TAB + r"(" + "|".join(str(c) for c in cids) + r")$"


def _create_schema(con):
    con.executescript(
        """
        DROP TABLE IF EXISTS syn;
        DROP TABLE IF EXISTS cid_parent;
        DROP TABLE IF EXISTS cid_smiles;
        CREATE TABLE syn (cid INTEGER, syn TEXT, syn_norm TEXT);
        CREATE TABLE cid_parent (cid INTEGER PRIMARY KEY, parent_cid INTEGER);
        CREATE TABLE cid_smiles (cid INTEGER PRIMARY KEY, smiles TEXT);
        """
    )


def _index_and_finish(con):
    print("  building indexes ...", flush=True)
    con.executescript(
        """
        CREATE INDEX idx_syn_norm ON syn(syn_norm);
        CREATE INDEX idx_syn_cid ON syn(cid);
        CREATE INDEX idx_parent ON cid_parent(parent_cid);
        """
    )
    con.commit()
    con.execute("PRAGMA optimize")
    con.commit()


def _bulk_setup(con):
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-200000")  # ~200MB page cache during build


def build_mini(con, src, names, unfiltered=False):
    """Tiny index over ``names`` + their salt-union siblings. Every pass is
    grep-prefiltered (C), so Python only sees matching lines."""
    syn_f = _flat(src, "synonym", unfiltered)
    par_f = _flat(src, "parent")
    smi_f = _flat(src, "smiles")
    targets = {normalize_key(n) for n in names if normalize_key(n)}
    print(f"mini build for {len(targets)} normalized names: {sorted(targets)}", flush=True)

    # 1. names -> candidate CIDs. Fast fixed-string grep prefilter, then keep only
    #    rows whose synonym normalizes EXACTLY to a target (drops salts/substring hits).
    target_cids = set()
    for cid, syn in grep_fixed_stream(syn_f, _name_terms(names)):
        if normalize_key(syn) in targets:
            target_cids.add(cid)
    print(f"  pass1: {len(target_cids)} candidate CIDs", flush=True)
    if not target_cids:
        sys.exit("no candidate CIDs found for the given names")

    # 2. parent of each candidate
    parents = {}
    for cid, par in grep_stream(par_f, _cid_head_pattern(target_cids)):
        parents[cid] = par
    for c in target_cids:
        parents.setdefault(c, c)  # no parent row => its own parent
    P = set(parents.values())

    # 3. salt-union siblings: every CID whose parent is in P, capped per parent so a
    #    tiny common moiety (e.g. glycine — parent of thousands) can't explode the set.
    SIB_CAP = 50
    union = set(target_cids) | P
    cid_parent = dict(parents)
    per_parent = {}
    for cid, par in grep_stream(par_f, _cid_tail_pattern(P)):
        if per_parent.get(par, 0) >= SIB_CAP:
            continue
        per_parent[par] = per_parent.get(par, 0) + 1
        union.add(cid)
        cid_parent[cid] = par
    for c in P:
        cid_parent.setdefault(c, c)
    print(f"  pass2-3: salt union = {len(union)} CIDs", flush=True)

    # 4-5. synonyms + SMILES for the union. Stream the whole (CID-sorted) file and
    #      test set membership in Python — robust, and avoids a giant grep alternation.
    rows = [(cid, syn, normalize_key(syn)) for cid, syn in stream(syn_f) if cid in union]
    con.executemany("INSERT INTO syn VALUES (?,?,?)", rows)
    print(f"  pass4: {len(rows)} synonym rows", flush=True)

    con.executemany("INSERT OR IGNORE INTO cid_parent VALUES (?,?)", list(cid_parent.items()))
    smi = [(cid, s) for cid, s in stream(smi_f) if cid in union]
    con.executemany("INSERT OR IGNORE INTO cid_smiles VALUES (?,?)", smi)
    print(f"  pass5: {len(smi)} SMILES rows", flush=True)
    con.commit()


def build_full(con, src, unfiltered=False, sample_cids=None):
    syn_f = _flat(src, "synonym", unfiltered)
    par_f = _flat(src, "parent")
    smi_f = _flat(src, "smiles")

    # 1. all synonyms; collect the "named" CID set.
    named = set()
    batch, n = [], 0
    t0 = time.time()
    for cid, syn in stream(syn_f):
        if sample_cids and cid > sample_cids:
            break
        named.add(cid)
        batch.append((cid, syn, fast_norm(syn)))
        if len(batch) >= 50000:
            con.executemany("INSERT INTO syn VALUES (?,?,?)", batch)
            n += len(batch)
            batch = []
            if n % 5_000_000 == 0:
                print(f"  syn {n:,} rows, {len(named):,} CIDs, {time.time()-t0:.0f}s", flush=True)
    if batch:
        con.executemany("INSERT INTO syn VALUES (?,?,?)", batch)
        n += len(batch)
    con.commit()
    print(f"  syn done: {n:,} rows / {len(named):,} named CIDs", flush=True)

    # 2. parents for named CIDs; collect parent CIDs (needed for structure).
    parents_needed = set()
    batch = []
    for cid, par in stream(par_f):
        if sample_cids and cid > sample_cids:
            break
        if cid in named:
            batch.append((cid, par))
            parents_needed.add(par)
            if len(batch) >= 50000:
                con.executemany("INSERT OR IGNORE INTO cid_parent VALUES (?,?)", batch)
                batch = []
    if batch:
        con.executemany("INSERT OR IGNORE INTO cid_parent VALUES (?,?)", batch)
    con.commit()
    keep = named | parents_needed
    print(f"  cid_parent done; keep set = {len(keep):,} CIDs", flush=True)

    # 3. SMILES for named CIDs + their parents only.
    batch, m = [], 0
    for cid, s in stream(smi_f):
        if sample_cids and cid > sample_cids:
            break
        if cid in keep:
            batch.append((cid, s))
            if len(batch) >= 50000:
                con.executemany("INSERT OR IGNORE INTO cid_smiles VALUES (?,?)", batch)
                m += len(batch)
                batch = []
    if batch:
        con.executemany("INSERT OR IGNORE INTO cid_smiles VALUES (?,?)", batch)
        m += len(batch)
    con.commit()
    print(f"  cid_smiles done: {m:,} rows", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=os.getenv("PUBCHEM_FLAT_DIR", DEFAULT_SRC))
    ap.add_argument("--out")
    ap.add_argument("--mini", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--names", help="comma-separated drug names (mini mode)")
    ap.add_argument("--names-file", help="file with one drug name per line (mini mode)")
    ap.add_argument("--unfiltered", action="store_true", help="use CID-Synonym-unfiltered (more recall)")
    ap.add_argument("--sample-cids", type=int, default=2_000_000, help="calibrate: only CIDs <= this")
    args = ap.parse_args()

    if not (args.mini or args.full or args.calibrate):
        ap.error("choose one of --mini / --full / --calibrate")

    # Verify required input flat files exist BEFORE removing/overwriting any existing index.
    _flat(args.src, "synonym", args.unfiltered)
    _flat(args.src, "parent")
    _flat(args.src, "smiles")

    default_out = "pubchem_offline_mini.sqlite" if args.mini else "pubchem_offline.sqlite"
    out = args.out or os.path.join(_REPO_ROOT, "data", "pubchem", default_out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    print(f"src={args.src}\nout={out}\nsynonyms={'unfiltered' if args.unfiltered else 'filtered'}", flush=True)

    con = sqlite3.connect(out)
    _bulk_setup(con)
    _create_schema(con)
    t0 = time.time()
    if args.mini:
        names = []
        if args.names:
            names += [x.strip() for x in args.names.split(",")]
        if args.names_file:
            with open(args.names_file) as fh:
                names += [ln.strip() for ln in fh if ln.strip()]
        if not names:
            ap.error("--mini needs --names or --names-file")
        build_mini(con, args.src, names, args.unfiltered)
    elif args.calibrate:
        build_full(con, args.src, args.unfiltered, sample_cids=args.sample_cids)
    else:
        build_full(con, args.src, args.unfiltered)
    _index_and_finish(con)
    con.close()

    size_gb = os.path.getsize(out) / 1e9
    print(f"\ndone in {time.time()-t0:.0f}s | size {size_gb:.2f} GB | {out}", flush=True)
    if args.calibrate:
        # rows scanned were CIDs <= sample_cids out of ~119M total compounds.
        print(f"calibrate: scanned CIDs <= {args.sample_cids:,}; "
              f"extrapolate ~x{119_000_000/args.sample_cids:.0f} for full (very rough).", flush=True)


if __name__ == "__main__":
    main()
