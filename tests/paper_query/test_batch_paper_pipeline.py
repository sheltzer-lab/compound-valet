"""Offline regression tests for scripts/batch_paper_pipeline.py and core/async_utils.py."""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "batch_paper_pipeline.py")


def _make_dummy_pdf(folder, name="Smith et al. - 2025 - Test compound paper.pdf"):
    path = folder / name
    path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    return path


def test_dry_run_fresh_folder_is_strictly_read_only(tmp_path):
    _make_dummy_pdf(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))

    res = subprocess.run(
        [sys.executable, SCRIPT, str(tmp_path), "--dry_run"],
        capture_output=True,
        text=True,
        check=True,
    )
    after = sorted(p.name for p in tmp_path.rglob("*"))
    assert after == before
    assert "[DRY RUN] Would submit 1 new requests" in (res.stdout + res.stderr)


def test_dry_run_from_gemini_parsed_state_does_not_crash_or_clobber(tmp_path):
    _make_dummy_pdf(tmp_path)
    res_dir = tmp_path / "_pipeline_results"
    res_dir.mkdir()
    state_file = tmp_path / "pipeline_state.json"
    state_content = json.dumps({"stage": "gemini_parsed", "files": {}, "job_id": "job-1"})
    state_file.write_text(state_content)
    s1 = [{"filename": "Smith et al. - 2025 - Test compound paper.pdf", "author": "Smith et al.", "year": 2025, "title": "Test"}]
    (res_dir / "stage1_parsed.json").write_text(json.dumps(s1))
    (res_dir / "stage2_s2_results.json").write_text(json.dumps(s1))

    res = subprocess.run(
        [sys.executable, SCRIPT, str(tmp_path), "--dry_run"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert state_file.read_text() == state_content
    assert not (res_dir / "stage4_filtered.json").exists()
    assert not (res_dir / "stage5_reports").exists()


def test_empty_db_preflight_blocks_empty_and_allows_populated(tmp_path, monkeypatch):
    import core.db
    import scripts.batch_paper_pipeline as bpp

    db_path = tmp_path / "valet.db"
    monkeypatch.setattr(core.db, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(core.db, "_ENGINE", None)
    monkeypatch.setattr(bpp, "DATABASE_URL", f"sqlite:///{db_path}")

    with pytest.raises(RuntimeError, match="has no compound reports"):
        bpp._check_valet_db_populated()

    core.db.save_analysis("Imatinib", {"report": "VERIFIED", "structured_summary": {"final_verdict": "VERIFIED"}})
    assert bpp._check_valet_db_populated() == 1
    core.db._ENGINE.dispose()


def test_stage3_parse_handles_thought_parts_and_empty_parts(tmp_path):
    import scripts.batch_paper_pipeline as bpp

    res_dir = tmp_path / "_pipeline_results"
    res_dir.mkdir()
    raw_path = res_dir / "raw.jsonl"
    papers = [
        {"filename": "A - 2025 - Thinking.pdf", "author": "A", "year": 2025, "s2_venue": "Nature", "s2_pub_types": []},
        {"filename": "B - 2025 - EmptyParts.pdf", "author": "B", "year": 2025, "s2_venue": "Cell", "s2_pub_types": []},
    ]
    lines = [
        json.dumps({
            "custom_id": "A - 2025 - Thinking.pdf",
            "response": {
                "candidates": [{
                    "content": {
                        "parts": [
                            {"thought": True, "text": "internal chain of thought"},
                            {"text": json.dumps({"title": "Thinking Paper", "drugs": [{"name": "Imatinib"}], "additional_drugs": []})},
                        ]
                    }
                }]
            },
        }),
        json.dumps({
            "custom_id": "B - 2025 - EmptyParts.pdf",
            "response": {"candidates": [{"content": {"parts": []}}]},
        }),
    ]
    raw_path.write_text("\n".join(lines) + "\n")

    parsed = bpp.stage3_parse(str(tmp_path), papers, str(raw_path))
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Thinking Paper"
    assert parsed[0]["extracted_drugs"] == [{"name": "Imatinib"}]


def test_html_report_escapes_script_closing_tags(tmp_path):
    import scripts.batch_paper_pipeline as bpp

    report_dir = tmp_path / "stage5_reports"
    report_dir.mkdir()
    manuscripts = [{"Title": "Effect of </script><img src=x onerror=alert(1)> at <10 nM", "Journal": "J", "Year": 2025, "Author": "A", "Filename": "a.pdf", "drugCount": 0, "notInDbCount": 0}]
    bpp._generate_html_report(
        str(report_dir), manuscripts, [], {}, {}, {}, [], [],
        {"total_papers": 1, "papers_with_unverified": 0, "papers_with_actionable_alt": 0, "unique_drugs_in_db": 0, "unique_drugs_not_in_db": 0},
        {}, {}, {}, [],
    )
    html = (report_dir / "pipeline_report.html").read_text()
    assert "</script><img" not in html
    assert "<\\/script>" in html
    assert "function esc(" in html


def test_extract_vertex_output_uri():
    from types import SimpleNamespace
    from core.async_utils import _extract_vertex_output_uri

    job1 = SimpleNamespace(output_location="gs://b/out")
    assert _extract_vertex_output_uri(job1) == "gs://b/out"

    job2 = SimpleNamespace(dest=SimpleNamespace(gcs_uri="gs://b/dest"))
    assert _extract_vertex_output_uri(job2) == "gs://b/dest"

    job3 = SimpleNamespace(dest=SimpleNamespace(gcs_uri=["gs://b/list0"]))
    assert _extract_vertex_output_uri(job3) == "gs://b/list0"
