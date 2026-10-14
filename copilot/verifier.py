"""The verifier: no number reaches a reader unless it is in the evidence.

Ported in STRUCTURE from `reference/satquery-ai-main/tools/verifier.py` - the
same idea of re-checking the finished TEXT blindly rather than trusting the
generator - but the check itself is different, because Strandline's problem is
different. There, a caption was re-derived from a mask. Here, every number was
already computed by code and recorded as a fact before the text existed, so
the question is narrower and can be answered exactly:

    does every number in this text appear in the evidence store?

The method is masking, not parsing. Each allowed literal - a fact formatted
through `copilot/units.py`, a date, an OSM id, a run id - is struck out of a
working copy of the text, longest first, and only where it is not touching
another digit. Whatever digits are left over were not in the evidence, and the
answer is rejected. That catches:

  * a model that reworded a number,
  * a number planted in the QUESTION and echoed back,
  * a derived number (a percentage, a sum) that was never derived,
  * Devanagari digits, which are read as the numbers they are.

It also catches a number spelled out in words, for the words that are
unambiguously numbers. Nepali's `एक` ("one") and `छ` ("six") are also an
ordinary pronoun and the verb "is", so they are deliberately NOT on the list:
flagging them would reject correct Nepali prose. That gap is real and is
recorded in docs/open_items.md; digits, which is how the templates write every
Nepali number, are covered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from copilot.units import DIGIT_CLASS, NUMBER_RE, parse_number, to_ascii_digits

# Number words that cannot be anything but a number. English "one" is here
# with the two phrases where it is a pronoun carved out below.
NUMBER_WORDS_EN = (
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "thousand", "million", "dozen",
)

# Nepali words that are ONLY numbers. `एक` (one) and `छ` (six) are left out on
# purpose - see the module docstring.
NUMBER_WORDS_NE = (
    "दुई", "तीन", "चार", "पाँच", "सात", "आठ", "नौ", "दश", "दस",
    "एघार", "बाह्र", "तेह्र", "चौध", "पन्ध्र", "सोह्र", "सत्र", "अठार", "उन्नाइस",
    "बीस", "सय", "हजार", "लाख", "करोड",
)

# Where an English number word is a pronoun, not a count.
_EN_WORD_EXCEPTIONS = (re.compile(r"\bone of\b"), re.compile(r"\bno one\b"))

NUMBER_WORDS = {"en": NUMBER_WORDS_EN, "ne": NUMBER_WORDS_NE}

_MASK = "█"


@dataclass(frozen=True)
class Finding:
    """One thing in the text that is not backed by the evidence."""

    literal: str
    kind: str          # "digit" | "word"
    context: str

    def as_dict(self) -> dict:
        return {"literal": self.literal, "kind": self.kind, "context": self.context}


@dataclass
class VerificationResult:
    text: str
    findings: list = field(default_factory=list)
    matched: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        return {"ok": self.ok, "findings": [f.as_dict() for f in self.findings],
                "matched": list(self.matched)}


def tokens_of(literal: str) -> list:
    """(numeric token, offset) for every number inside an allowed literal.

    "Sentinel-1" carries the token "1" at offset 9; "2024-07-18" carries
    three. An index of these is what lets a text be checked in time
    proportional to the numbers IN IT rather than to the size of the
    evidence - which matters: a run with 2900 settlements has tens of
    thousands of allowed literals, and the first implementation re-scanned
    every one of them for every answer.
    """
    return [(m.group(0), m.start()) for m in NUMBER_RE.finditer(literal)]


def index_allowed(allowed) -> dict:
    """numeric token -> [(literal, offset of the token in it)]."""
    index: dict = {}
    for item in allowed:
        literal = str(item)
        if not literal.strip():
            continue
        for token, offset in tokens_of(literal):
            index.setdefault(token, []).append((literal, offset))
    return index


# Devanagari vowel signs are combining marks, and Python's `\b` treats a
# combining mark as a NON-word character - so `\bचार\b` matches inside
# `विचार` ("consideration"), because the `ि` in front of it looks like a word
# boundary. Matching against the whole Devanagari block instead is the fix;
# this build hit exactly that on a Nepali label.
_DEVANAGARI = r"ऀ-ॿ"


def _word_pattern(word: str, lang: str) -> str:
    if lang == "ne":
        return rf"(?<![{_DEVANAGARI}]){re.escape(word)}(?![{_DEVANAGARI}])"
    return rf"\b{re.escape(word)}\b"


def _context(text: str, start: int, end: int, width: int = 28) -> str:
    return text[max(0, start - width):min(len(text), end + width)].replace("\n", " ").strip()


def verify(text: str, allowed, lang: str = "en") -> VerificationResult:
    """Check that every number in `text` is one of `allowed`.

    `allowed` is the set of literals the evidence permits, already formatted
    for `lang` (see `EvidenceStore.allowed_strings`).
    """
    result = VerificationResult(text=text)
    index = allowed if isinstance(allowed, dict) else index_allowed(allowed)

    # Every MAXIMAL numeric token in the text, checked one at a time. Maximal
    # is the point: the allowed value 100.0 does not excuse 5100.05, because
    # the token found there is "5100.05" and nothing in the evidence spells
    # that.
    working = list(text)
    for match in NUMBER_RE.finditer(text):
        token, start = match.group(0), match.start()
        for literal, offset in index.get(token, ()):
            begin = start - offset
            if begin >= 0 and text[begin:begin + len(literal)] == literal:
                result.matched.append(literal)
                working[match.start():match.end()] = _MASK * (match.end() - match.start())
                break
        else:
            result.findings.append(Finding(
                literal=to_ascii_digits(token), kind="digit",
                context=_context(text, match.start(), match.end())))
    working = "".join(working)

    for word in NUMBER_WORDS.get(lang, ()):  # pragma: no branch - both langs listed
        for match in re.finditer(_word_pattern(word, lang), working, flags=re.IGNORECASE):
            if lang == "en" and any(
                    exc.search(working[max(0, match.start() - 8):match.end() + 8].lower())
                    for exc in _EN_WORD_EXCEPTIONS):
                continue
            result.findings.append(Finding(
                literal=word, kind="word",
                context=_context(text, match.start(), match.end())))
    return result


def check_template(template: str, allowed, lang: str = "en") -> VerificationResult:
    """The same check, run on a template BEFORE its placeholders are filled.

    A template is the model's only output, and it is supposed to contain no
    number at all - every number arrives from a fact through a placeholder. So
    the placeholders are blanked out and whatever number survives was written
    by the model. Allowed literals are still honoured, because a product name
    such as "Sentinel-2" is evidence-backed vocabulary, not a quantity.
    """
    blanked = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", _MASK, template)
    return verify(blanked, allowed, lang)


def numbers_in(text: str) -> list:
    """Every number in `text`, as floats, in either script. For tests and for
    the evaluation's independent check."""
    return [n for n in (parse_number(m.group(0)) for m in NUMBER_RE.finditer(text))
            if n is not None]
