"""The PI-verified `mechanism` classification flows from the report's trailing
JSON block through process_pi_response into structured_data (and is stripped from
the rendered Markdown)."""

from agents.pi import process_pi_response


def test_process_pi_response_extracts_mechanism():
    report = (
        "## 1. Verification Conclusion\n"
        "**Verdict**: VERIFIED\n\n"
        "**Mechanism of Action**: The compound recruits an E3 ligase and drives "
        "degradation of the target. Classified as a Degrader.\n\n"
        "```json\n"
        "{\n"
        '  "final_verdict": "VERIFIED",\n'
        '  "target": "BRD4",\n'
        '  "mechanism": "Degrader"\n'
        "}\n"
        "```\n"
    )

    clean_text, structured = process_pi_response(report)

    assert structured["mechanism"] == "Degrader"
    assert structured["final_verdict"] == "VERIFIED"
    # The JSON block is stripped from the stored report text.
    assert "```json" not in clean_text
    assert '"mechanism"' not in clean_text
    # But the narrative discussion of the mechanism survives.
    assert "Degrader" in clean_text


def test_process_pi_response_without_mechanism_is_safe():
    """A report whose JSON omits mechanism yields no key (not a crash)."""
    report = (
        "**Verdict**: UNVERIFIED\n\n"
        "```json\n"
        '{ "final_verdict": "UNVERIFIED", "target": "unknown" }\n'
        "```\n"
    )
    _, structured = process_pi_response(report)
    assert structured.get("mechanism") is None


def test_process_pi_response_strips_leaked_mechanism_class_prose_line():
    """Mechanism class is a structured-only field; if the model/reviewers write it into the
    prose (they sometimes do), that line must be stripped so it never renders — while the
    Mechanism of Action narrative (and mid-sentence "mechanism class" mentions) survives and
    the structured `mechanism` value is unaffected."""
    report = (
        "## 1. Verification Conclusion\n"
        "**Verdict**: UNVERIFIED\n\n"
        "**Mechanism Class**: Unknown (the primary target is UNVERIFIED so the class is Unknown).\n\n"
        "## 3. Detailed Synthesis\n"
        "**Mechanism of Action**: Its mechanism class in cells appears inhibitory.\n\n"
        "```json\n"
        '{ "final_verdict": "UNVERIFIED", "target": "Unknown", "mechanism": "Unknown" }\n'
        "```\n"
    )
    clean_text, structured = process_pi_response(report)
    # the leaked structured-only field line is stripped from the prose
    assert "so the class is Unknown" not in clean_text
    # the Mechanism of Action narrative (incl. a mid-sentence "mechanism class") survives
    assert "Mechanism of Action" in clean_text
    assert "mechanism class in cells appears inhibitory" in clean_text
    # structured mechanism still comes from the JSON block
    assert structured["mechanism"] == "Unknown"
