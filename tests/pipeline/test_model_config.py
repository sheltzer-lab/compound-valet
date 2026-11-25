"""Tests for core/model_config.py"""

from core.model_config import GENERATION_CONFIG, SAFETY_SETTINGS


def test_generation_config_has_no_output_cap():
    assert "max_output_tokens" not in GENERATION_CONFIG   # no cap: thinking counts against it
    for key in ("temperature", "top_p", "candidate_count"):
        assert key in GENERATION_CONFIG


def test_safety_settings():
    assert {s.category for s in SAFETY_SETTINGS} == {
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_HARASSMENT",
    }
    assert all(s.threshold == "BLOCK_ONLY_HIGH" for s in SAFETY_SETTINGS)
