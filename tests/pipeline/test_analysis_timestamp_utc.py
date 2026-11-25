"""save_analysis must stamp UTC, on every host.

`timestamp` used to be `datetime.now().isoformat()` — naive local time, so rows written on
machines in different time zones could not be compared or sorted.

Isolation comes from the autouse `_isolated_db` fixture in conftest.py — do NOT build a
temp DB by hand here, and never delete `core.*` from sys.modules: that leaves later tests
sharing one engine and they fail on UNIQUE constraints.
"""
import datetime

from core.db import get_analysis, save_analysis


def test_save_analysis_stamps_utc_not_local():
    before = datetime.datetime.utcnow()
    save_analysis("TZ-TEST", {
        "report": "r",
        "structured_summary": {"final_verdict": "VERIFIED"},
    })
    after = datetime.datetime.utcnow()

    ts = datetime.datetime.fromisoformat(get_analysis("TZ-TEST")["timestamp"])
    slack = datetime.timedelta(seconds=5)
    assert before - slack <= ts <= after + slack, (
        f"timestamp {ts} is outside the utcnow window {before}..{after} — "
        "save_analysis is stamping local time again"
    )


def test_update_path_also_stamps_utc():
    """The update branch has its own timestamp write; cover it too."""
    save_analysis("TZ-TEST-2", {"report": "first",
                                "structured_summary": {"final_verdict": "VERIFIED"}})
    before = datetime.datetime.utcnow()
    save_analysis("TZ-TEST-2", {"report": "second",
                                "structured_summary": {"final_verdict": "UNVERIFIED"}})
    after = datetime.datetime.utcnow()

    ts = datetime.datetime.fromisoformat(get_analysis("TZ-TEST-2")["timestamp"])
    slack = datetime.timedelta(seconds=5)
    assert before - slack <= ts <= after + slack, (
        f"updated timestamp {ts} is outside the utcnow window — update path stamps local"
    )
