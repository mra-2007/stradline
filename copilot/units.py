"""How a number is written down, and how it is read back.

ONE formatter, used by the evidence store, by the template filler, by the
situation report and by the verifier. That is the whole point of the module:
the verifier can only match a number in a finished answer against the evidence
if both sides spell it the same way, so there must not be a second place where
a number is turned into text.

Nepali answers may show Devanagari digits. A Devanagari digit string is the
same number as its ASCII twin - `to_ascii_digits` undoes the mapping - so the
verifier reads both without caring which script an answer chose.
"""

from __future__ import annotations

import math
import re

from strandline.config import DEVANAGARI_DIGITS, DISPLAY_PRECISION, THOUSANDS_SEPARATOR

# Units whose values are plain text, not quantities: formatted verbatim and
# allowed in an answer as themselves (a date, an OSM id, a class name).
TEXT_UNITS = ("date", "text", "id", "class", "status")

# Of those, the only one whose digits are DIGITS and may be shown in
# Devanagari. A name, an id or a status is transcribed verbatim in every
# language: turning the "1" of "Sentinel-1" into "१" would invent a mission.
TRANSLITERATED_TEXT_UNITS = ("date",)

_ASCII_TO_DEV = {ord(str(i)): DEVANAGARI_DIGITS[i] for i in range(10)}
_DEV_TO_ASCII = {ord(DEVANAGARI_DIGITS[i]): str(i) for i in range(10)}

# Any run of digits in either script, with an optional decimal part. Used by
# the verifier to find every number a piece of text contains.
DIGIT_CLASS = r"[0-9" + DEVANAGARI_DIGITS + r"]"
NUMBER_RE = re.compile(rf"{DIGIT_CLASS}+(?:[.·]{DIGIT_CLASS}+)?")


class UnknownUnitError(ValueError):
    """A unit with no declared display precision. Never guess one: an
    undeclared precision means the verifier and the text could disagree."""


def precision(unit: str | None) -> int:
    """Decimals for `unit`. Unitless counts are integers."""
    if unit is None:
        return 0
    if unit in TEXT_UNITS:
        raise UnknownUnitError(f"{unit!r} is a text unit, not a quantity")
    try:
        return DISPLAY_PRECISION[unit]
    except KeyError as err:
        raise UnknownUnitError(
            f"no display precision declared for unit {unit!r}; add it to "
            f"strandline.config.DISPLAY_PRECISION rather than formatting it ad hoc"
        ) from err


def format_value(value, unit: str | None, lang: str = "en") -> str:
    """The ONE spelling of `value` in `unit`. Nepali gets Devanagari digits.

    A None value is written as an em dash: it is not a number, and it must not
    become a zero on the way to a reader.
    """
    if value is None:
        return "—"
    if unit in TEXT_UNITS or isinstance(value, str):
        text = str(value)
        if lang == "ne" and unit not in TRANSLITERATED_TEXT_UNITS:
            return text
    elif isinstance(value, bool):
        text = "yes" if value else "no"
    else:
        number = float(value)
        if not math.isfinite(number):
            return "—"
        digits = precision(unit)
        text = f"{number:,.{digits}f}".replace(",", THOUSANDS_SEPARATOR)
        # -0.0 is a formatting artefact, never a measurement.
        if text.lstrip("-").strip("0.") == "" and text.startswith("-"):
            text = text[1:]
    return to_devanagari_digits(text) if lang == "ne" else text


def to_devanagari_digits(text: str) -> str:
    return text.translate(_ASCII_TO_DEV)


def to_ascii_digits(text: str) -> str:
    return text.translate(_DEV_TO_ASCII)


def parse_number(text: str) -> float | None:
    """Read a number written in either script back as a float."""
    ascii_text = to_ascii_digits(text).replace("·", ".")
    if THOUSANDS_SEPARATOR:
        ascii_text = ascii_text.replace(THOUSANDS_SEPARATOR, "")
    try:
        return float(ascii_text)
    except ValueError:
        return None
