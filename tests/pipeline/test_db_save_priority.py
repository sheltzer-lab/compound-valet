"""Test that save_analysis correctly stores reviewer_reports as JSON."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

from core.db import Base, AnalysisResult, save_analysis


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database and patch init_db to use it."""
    db_path = str(tmp_path / "test_save_priority.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with patch("core.db.init_db", return_value=Session):
        yield Session


def test_save_analysis_stores_reviewer_reports_dict(test_db):
    """reviewer_reports dict should be stored as-is in the JSON column."""
    drug_name = "TestDrugPriority"

    mock_output = {
        "report": "PI Report text",
        "reviewer_reports": {
            "Reviewer #1": "Reviewer #1 Content",
            "Reviewer #2": "Reviewer #2 Content",
        },
        "genetics_report": "Genetics...",
        "structured_summary": {},
        "usage": {},
        "cost": 0.0,
        "time": 1.0,
    }

    save_analysis(drug_name, mock_output)

    session = test_db()
    record = session.query(AnalysisResult).filter_by(drug_name=drug_name).first()
    assert record is not None

    stored = record.reviewer_reports
    assert isinstance(stored, dict)
    assert "Reviewer #1" in stored
    assert stored["Reviewer #2"] == "Reviewer #2 Content"
    session.close()


def test_save_analysis_promotes_mechanism(test_db):
    """`mechanism` in structured_summary is promoted to the mechanism column."""
    drug_name = "TestDrugMechanism"

    mock_output = {
        "report": "PI Report text",
        "structured_summary": {"mechanism": "Degrader", "target": "BRD4"},
        "usage": {},
        "cost": 0.0,
        "time": 1.0,
    }

    save_analysis(drug_name, mock_output)

    session = test_db()
    record = session.query(AnalysisResult).filter_by(drug_name=drug_name).first()
    assert record is not None
    assert record.mechanism == "Degrader"
    session.close()
