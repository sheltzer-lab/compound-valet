"""Regression tests for CLI side effects, DB initialization, and model/env configuration."""

import importlib
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_simple_batch_help_has_no_file_or_log_side_effects(tmp_path):
    workdir = tmp_path / "run"
    workdir.mkdir()
    res = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "simple_batch.py"), "--help"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.lstrip().startswith("usage:")
    assert list(workdir.iterdir()) == []


def test_init_db_creates_custom_parent_dir_and_not_data_dir(tmp_path, monkeypatch):
    import core.db

    monkeypatch.chdir(tmp_path)
    custom_db = tmp_path / "custom_subdir" / "valet.db"
    monkeypatch.setattr(core.db, "DATABASE_URL", f"sqlite:///{custom_db}")
    monkeypatch.setattr(core.db, "_ENGINE", None)

    Session = core.db.init_db()
    s = Session()
    s.close()
    core.db._ENGINE.dispose()

    assert custom_db.exists()
    assert not (tmp_path / "data").exists()


def test_init_db_raises_on_unwritable_sqlite_path(tmp_path, monkeypatch):
    import core.db

    # Point SQLite URL at a directory path (cannot be opened as a DB file).
    bad_dir = tmp_path / "is_a_dir"
    bad_dir.mkdir()
    monkeypatch.setattr(core.db, "DATABASE_URL", f"sqlite:///{bad_dir}")
    monkeypatch.setattr(core.db, "_ENGINE", None)

    with pytest.raises(Exception):
        core.db.init_db()
    assert core.db._ENGINE is None


def test_target_match_model_respects_env_override(monkeypatch):
    import core.target_matching as tm

    monkeypatch.setenv("GEMINI_TARGET_MATCH_MODEL", "gemini-custom-flash")
    try:
        reloaded = importlib.reload(tm)
        assert reloaded.TARGET_MATCH_MODEL == "gemini-custom-flash"
    finally:
        monkeypatch.delenv("GEMINI_TARGET_MATCH_MODEL", raising=False)
        importlib.reload(tm)


def test_simple_batch_uses_report_model_not_hardcoded_preview():
    import simple_batch

    assert not hasattr(simple_batch, "MODEL")
    assert simple_batch.REPORT_MODEL is not None


def test_build_pubchem_index_loads_dotenv(tmp_path):
    """PUBCHEM_FLAT_DIR in .env must be picked up by scripts/build_pubchem_index.py,
    and missing flat files must fail before touching any output index."""
    flat_dir = tmp_path / "custom_flat"
    flat_dir.mkdir()
    out_db = tmp_path / "out.sqlite"
    (tmp_path / ".env").write_text(f"PUBCHEM_FLAT_DIR={flat_dir}\n")
    env = {k: v for k, v in os.environ.items() if k != "PUBCHEM_FLAT_DIR"}

    res = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "build_pubchem_index.py"),
         "--full", "--out", str(out_db)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert str(flat_dir) in (res.stdout + res.stderr)
    assert not out_db.exists()
