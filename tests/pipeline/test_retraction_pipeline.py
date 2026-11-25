"""Unit tests for the live single-drug retraction path.

Covers the live per-DOI retraction parse + flagging, the DB round-trip, and the
PI-prompt tag. The autouse _isolated_db fixture (tests/pipeline/conftest.py) gives each test
a fresh empty SQLite DB. The live-flagging tests mock CrossRef — the live path
reads status directly from CrossRef and depends on no local index.
"""
import asyncio

from core.db import init_db, save_verified_papers, get_verified_papers
from core.retraction_live import parse_retraction, flag_retractions_live, extract_doi
from core.references.clients import CrossRefClient
from core.prompt_builder import inject_verified_sources


def _update(doi, type_="retraction", src="retraction-watch", dp=(2010, 2, 6)):
    return {"DOI": doi, "type": type_, "source": src, "updated": {"date-parts": [list(dp)]}}


# ── pure helpers ───────────────────────────────────────────────────────────
class TestDoiHelpers:
    def test_extract_doi(self):
        assert extract_doi("https://doi.org/10.1038/s41586-020-2008-3") == "10.1038/s41586-020-2008-3"
        assert extract_doi("doi:10.1038/x") == "10.1038/x"
        assert extract_doi("https://doi.org/10.1038/abc?foo=bar") == "10.1038/abc"
        assert extract_doi("https://example.com/article") is None
        assert extract_doi("") is None


# ── pure parse: tri-state + updated-by direction/type guard ────────────────
class TestParseRetraction:
    def test_retraction_entry_is_true(self):
        r = parse_retraction({"updated-by": [_update("10.n/1", dp=(2010, 2, 6))]})
        assert r["retracted"] is True
        assert r["retraction_date"] == "2010-02-06"
        assert r["retraction_notice_doi"] == "10.n/1"

    def test_correction_only_is_false(self):
        # guards the type filter: a non-retraction update must NOT flag as retracted
        r = parse_retraction({"updated-by": [_update("10.n/c", type_="correction")]})
        assert r["retracted"] is False

    def test_earliest_retraction_kept_when_multiple(self):
        r = parse_retraction({"updated-by": [_update("10.n/2", dp=(2018, 1, 1)),
                                             _update("10.n/1", dp=(2016, 1, 1))]})
        assert r["retracted"] is True
        assert r["retraction_date"] == "2016-01-01"
        assert r["retraction_notice_doi"] == "10.n/1"

    def test_clean_record_is_false(self):
        assert parse_retraction({"title": ["ok"]})["retracted"] is False

    def test_failed_fetch_is_none(self):
        # get_work_by_doi returns None on 404 / network / timeout -> unknown, never False
        r = parse_retraction(None)
        assert r["retracted"] is None
        assert r["retraction_date"] is None and r["retraction_notice_doi"] is None


# ── live flagging (tri-state) via mocked CrossRef; index left EMPTY ─────────
class TestFlagRetractionsLive:
    def test_tristate_and_url_fallback(self, monkeypatch):
        # Retraction status comes from the mocked CrossRef only.
        # flag_retractions_live batches via get_works_by_dois -> {doi: record}; a DOI absent
        # from the returned dict means "not found" (parse_retraction(None) -> unknown).
        async def fake_get_works_by_dois(self, dois, session, chunk_size=100):
            recs = {
                "10.1016/bad": {"updated-by": [_update("10.n/1", dp=(2010, 2, 6))]},   # retracted
                "10.1234/bad2": {"updated-by": [_update("10.n/2", dp=(2011, 1, 1))]},  # retracted (URL)
                "10.1038/clean": {"title": ["ok"]},                                    # 200, clean
            }
            return {d: recs[d] for d in dois if d in recs}   # "10.1038/missing" absent -> None
        monkeypatch.setattr(CrossRefClient, "get_works_by_dois", fake_get_works_by_dois)

        papers = [
            {"doi": "10.1016/bad"},                        # True (doi field)
            {"doi": "10.1038/clean"},                      # False (checked, not retracted)
            {"url": "https://doi.org/10.1234/bad2"},       # True via URL fallback
            {"doi": "10.1038/missing"},                    # None (200 miss -> get returns None)
            {"url": "https://example.com/no-doi"},         # None (no DOI -> no call)
        ]
        asyncio.run(flag_retractions_live(papers))
        assert papers[0]["retracted"] is True and papers[0]["retraction_date"] == "2010-02-06"
        assert papers[0]["retraction_notice_doi"] == "10.n/1"
        assert papers[1]["retracted"] is False
        assert papers[2]["retracted"] is True             # resolved from the URL
        assert papers[3]["retracted"] is None             # failed/missing lookup
        assert papers[4]["retracted"] is None             # no DOI to check


# ── DB round-trip (None must persist as None) ──────────────────────────────
class TestSaveGetRoundtrip:
    def test_tristate_persists(self):
        Session = init_db()
        s = Session()
        save_verified_papers(s, "D", [
            {"title": "a", "authors": ["A"], "year": "1", "url": "u1", "citation": "c",
             "retracted": True, "retraction_date": "2010-02-06", "retraction_notice_doi": "10.n/1"},
            {"title": "b", "authors": ["B"], "year": "2", "url": "u2", "citation": "c",
             "retracted": False},
            {"title": "c", "authors": ["C"], "year": "3", "url": "u3", "citation": "c",
             "retracted": None},
        ])
        s.commit()
        s.close()
        got = {g["title"]: g for g in get_verified_papers("D")}
        assert got["a"]["retracted"] is True and got["a"]["retraction_date"] == "2010-02-06"
        assert got["b"]["retracted"] is False
        assert got["c"]["retracted"] is None              # the bug fix: unknown stays NULL


# ── PI-prompt tag (only True is surfaced) ──────────────────────────────────
class TestPromptTag:
    def test_only_true_tagged(self):
        Session = init_db()
        s = Session()
        save_verified_papers(s, "D", [
            {"title": "R", "authors": ["A"], "year": "1", "url": "u1", "citation": "c",
             "retracted": True, "retraction_date": "2010-02-06", "retraction_notice_doi": "10.n/1"},
            {"title": "C", "authors": ["B"], "year": "2", "url": "u2", "citation": "c",
             "retracted": False},
            {"title": "U", "authors": ["E"], "year": "3", "url": "u3", "citation": "c",
             "retracted": None},
        ])
        s.commit()
        s.close()
        out = inject_verified_sources("{{verified_sources}}", "D")
        assert out.count("[RETRACTED]") == 1              # only the True paper
        assert "2010-02-06" in out
        assert all(t in out for t in ("R", "C", "U"))     # clean/unknown still listed, untagged
