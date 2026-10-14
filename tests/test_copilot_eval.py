"""The scoreboard's OWN number check, which must be as strict as the verifier.

`eval/copilot_eval.py` re-implements "strike out every literal the evidence
allows and see what digits are left" on purpose, so that a copilot cannot
agree with itself. Re-implemented means it can also repeat a mistake: both
sides once read "-120.5" as the token "120.5", found the positive value in
the evidence and called a flipped sign backed (2026-10-14 audit, finding 2).
These tests hold the scoreboard to the signed rule independently of
`tests/test_copilot_verifier.py`, which holds the copilot to it.
"""

from copilot.units import to_devanagari_digits
from eval.copilot_eval import unmatched_numbers


def test_a_number_from_the_evidence_is_matched():
    assert unmatched_numbers("Flood water covers 100.0 ha.", {"100.0"}) == []


def test_a_flipped_sign_is_reported_as_unmatched():
    assert unmatched_numbers("The extent fell to -100.0 ha.", {"100.0"}) == ["-100.0"]


def test_a_typographic_minus_counts_as_a_sign():
    assert unmatched_numbers("fell to −100.0 ha", {"100.0"}) == ["−100.0"]


def test_a_sign_in_front_of_devanagari_digits_counts():
    text = "-" + to_devanagari_digits("100.0")
    assert unmatched_numbers(f"क्षे. {text}", {to_devanagari_digits("100.0")}) \
        == ["-100.0"]


def test_a_negative_value_passes_only_when_the_evidence_is_negative():
    assert unmatched_numbers("fell by -2.5 m", {"-2.5"}) == []
    assert unmatched_numbers("fell by 2.5 m", {"-2.5"}) == ["2.5"]


def test_a_hyphen_inside_an_allowed_literal_is_part_of_it():
    """"Sentinel-1" and a date must keep working: the hyphen there is not a
    sign, and the position check is what tells the two apart."""
    allowed = {"Sentinel-1", "2024-07-18", "way/5001"}
    assert unmatched_numbers("Built from Sentinel-1 on 2024-07-18 (way/5001).", allowed) == []
    assert unmatched_numbers("-1 bridge is out.", allowed) == ["-1"]
