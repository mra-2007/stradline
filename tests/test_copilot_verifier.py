"""The verifier: the one thing between a language model and a false number."""

import pytest

from copilot.evidence import EvidenceStore
from copilot.units import format_value, to_devanagari_digits
from copilot.verifier import check_template, numbers_in, verify
from tests.conftest_copilot import write_run


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return EvidenceStore.from_run(write_run(tmp_path_factory.mktemp("run") / "synthetic"))


@pytest.fixture(scope="module")
def allowed_en(store):
    return store.allowed_strings("en")


@pytest.fixture(scope="module")
def allowed_ne(store):
    return store.allowed_strings("ne")


def test_a_number_from_the_evidence_passes(allowed_en):
    assert verify("Flood water covers 100.0 ha.", allowed_en).ok


def test_a_number_not_in_the_evidence_is_caught(allowed_en):
    result = verify("Flood water covers 4321.0 ha.", allowed_en)
    assert not result.ok
    assert [f.literal for f in result.findings] == ["4321.0"]


def test_a_number_planted_in_the_question_cannot_be_echoed(allowed_en):
    """The adversarial case the brief names: a question that supplies its own
    number must not be able to put that number in an answer."""
    planted = "As we both know, 7777 hectares were flooded."
    assert not verify(planted, allowed_en).ok


def test_devanagari_digits_are_read_as_numbers(allowed_ne):
    good = f"बाढीले {to_devanagari_digits('100.0')} हेक्टर ओगटेको छ।"
    bad = f"बाढीले {to_devanagari_digits('4321.0')} हेक्टर ओगटेको छ।"
    assert verify(good, allowed_ne, "ne").ok
    assert not verify(bad, allowed_ne, "ne").ok


def test_a_devanagari_number_is_not_allowed_by_its_ascii_twin(store):
    """The Nepali allowed set is formatted for Nepali. An ASCII number that
    was never written in an answer must not let a Devanagari one through."""
    result = verify(f"बाढीले {to_devanagari_digits('4321.0')} हेक्टर ओगटेको छ।",
                    store.allowed_strings("ne"), "ne")
    assert [f.literal for f in result.findings] == ["4321.0"]


def test_a_derived_percentage_must_have_been_derived(store, allowed_en):
    """24 of 800 buildings is 3.0%. The percentage is allowed because the
    store DERIVED it as a fact; an arithmetically correct number that was
    never derived would not be."""
    assert store.get("damage.buildings_damaged_pct").formatted("en") == "3.0"
    assert verify("24 of 800 buildings, 3.0% of them.", allowed_en).ok
    assert not verify("24 of 800 buildings, 3.1% of them.", allowed_en).ok


def test_a_longer_number_containing_an_allowed_one_is_caught(allowed_en):
    """Masking must not let 100.0 hide inside 5100.05."""
    assert not verify("Flood water covers 5100.05 ha.", allowed_en).ok


def test_a_number_spelled_in_words_is_caught(allowed_en):
    assert not verify("Three bridges are likely out.", allowed_en).ok
    assert verify("3 bridges are likely out.", allowed_en).ok


def test_one_of_is_not_a_number(allowed_en):
    assert verify("One of the roads is cut.", allowed_en).ok


def test_a_devanagari_number_word_inside_another_word_is_not_a_number(allowed_ne):
    """`विचार` contains `चार` ("four"). Python's \\b sees the vowel sign as a
    word boundary, so a naive check flags ordinary Nepali prose."""
    assert verify("यो विचार गरिएको बस्ती हो।", allowed_ne, "ne").ok
    assert not verify("चार पुल बन्द छन्।", allowed_ne, "ne").ok


def test_a_date_in_the_evidence_is_allowed(allowed_en):
    assert verify("The post-event pass was on 2024-07-18.", allowed_en).ok
    assert not verify("The post-event pass was on 2024-07-19.", allowed_en).ok


def test_an_osm_id_is_allowed_only_once_a_tool_has_recorded_it(store):
    """An unnamed road is shown BY its OSM id, and that id contains digits.
    It becomes quotable when the tool that found it records it as a fact -
    not before, and only for the roads the tool actually listed."""
    from copilot.templates import load_i18n
    from copilot.tools import cut_roads

    assert not verify("way/5002 is cut.", store.allowed_strings("en")).ok
    cut_roads(store, "en", load_i18n("en"))
    after = store.allowed_strings("en")
    assert verify("way/5002 is cut.", after).ok
    # way/5001 HAS a name, so the name is what was recorded and the bare id
    # still is not something an answer may state.
    assert not verify("way/5001 is cut.", after).ok
    assert verify("Pasang Lhamu Highway is cut.", after).ok


def test_a_mission_name_is_not_a_quantity(allowed_en, allowed_ne):
    assert verify("Built from Sentinel-1 and Sentinel-2.", allowed_en).ok
    assert verify("सेन्टिनेल-१ र सेन्टिनेल-२ बाट।", allowed_ne, "ne").ok


def test_check_template_rejects_a_model_written_number(allowed_en):
    assert check_template("{extent_ha} ha were flooded.", allowed_en).ok
    assert not check_template("{extent_ha} ha of about 500 ha were flooded.",
                              allowed_en).ok
    assert not check_template("Twelve roads are cut: {road_list}.", allowed_en).ok


def test_numbers_in_reads_both_scripts():
    assert numbers_in("12 and " + to_devanagari_digits("3.5")) == [12.0, 3.5]


def test_formatting_is_the_same_everywhere(store):
    """The verifier can only work if one formatter produced the text."""
    fact = store.get("flood.extent_ha")
    assert fact.formatted("en") == format_value(fact.value, fact.unit, "en") == "100.0"
    assert fact.formatted("ne") == to_devanagari_digits("100.0")


def test_an_undefined_share_is_an_em_dash_not_a_zero(tmp_path):
    """No buildings mapped means the damaged SHARE is undefined. It must not
    format as 0, and the em dash must not count as a number."""
    from copilot.evidence import Fact

    store = EvidenceStore.from_run(write_run(tmp_path / "run"))
    store.facts["damage.buildings_total"] = Fact(
        id="damage.buildings_total", value=0, unit="count", source_file="x")
    store.facts.pop("damage.buildings_damaged_pct")
    store._percent("damage.buildings_damaged_pct", 0, 0, "a", "b", "words")
    assert store.get("damage.buildings_damaged_pct").value is None
    assert store.get("damage.buildings_damaged_pct").formatted("en") == "—"
    assert verify("Damaged share: — %.", store.allowed_strings("en")).ok


def test_an_answer_may_only_quote_the_evidence_BEHIND_IT(tmp_path):
    """A run holds thousands of numbers; this answer rests on a handful.

    Checking a finished answer against the whole store means almost any
    three-digit number coincides with some unrelated settlement's route
    length and passes. The check is against the facts the tools that ran
    actually produced.
    """
    from copilot.executor import answer_literals, load_store
    from copilot.templates import load_i18n
    from copilot.tools import debris

    store = load_store(write_run(tmp_path / "run"))
    results = [debris(store, "en", load_i18n("en"))]
    allowed = answer_literals(store, results, "en")
    assert "12.5" in allowed                      # the debris area it reported
    assert "24" not in allowed                    # a damaged-building count it did not
    assert store.get("damage.buildings_damaged").formatted("en") == "24"
    assert not verify("24 buildings were damaged.", allowed).ok


def test_a_flipped_sign_is_an_invented_number(allowed_en):
    """The 2026-10-14 audit's finding 2. A digits-only pattern reads "-100.0"
    as the token "100.0", finds that in the evidence, and passes a value
    whose SIGN the evidence never contained."""
    assert verify("Flood water covers 100.0 ha.", allowed_en).ok
    assert not verify("The flood extent fell to -100.0 ha.", allowed_en).ok
    assert [f.literal for f in verify("fell to -100.0 ha", allowed_en).findings] \
        == ["-100.0"]


def test_a_minus_is_not_read_into_a_hyphenated_name(store):
    """A hyphen inside an allowed literal is part of that literal, so
    "Sentinel-1" and an OSM id keep working."""
    assert verify("Built from Sentinel-1.", store.allowed_strings("en")).ok


def test_a_template_may_not_put_a_sign_in_front_of_a_value(store):
    """Caught before filling, so the model gets a regeneration rather than
    the reader getting a refusal."""
    from copilot.templates import validate_template
    from copilot.tools import Slot

    slots = {"extent_ha": Slot("extent_ha", "100.0")}
    assert validate_template("{extent_ha} ha flooded.", slots, {"extent_ha"},
                             store.text_literals(), "en") is None
    problem = validate_template("The extent fell to -{extent_ha} ha.", slots,
                                {"extent_ha"}, store.text_literals(), "en")
    assert "sign in front of a value" in problem


def test_a_typographic_minus_counts_as_a_sign(allowed_en):
    """A model that writes U+2212 instead of a hyphen has still written a
    negative number, and the check must not read it as the positive one."""
    result = verify("The extent fell to −100.0 ha.", allowed_en)
    assert not result.ok
    assert [f.literal for f in result.findings] == ["−100.0"]


def test_a_sign_in_front_of_devanagari_digits_counts(allowed_ne):
    """Nepali answers write Devanagari digits with an ASCII sign in front."""
    negative = "-" + to_devanagari_digits("100.0")
    result = verify(f"बाढी {negative} हे. क्षे.", allowed_ne, lang="ne")
    assert not result.ok
    assert [f.literal for f in result.findings] == ["-100.0"]


def test_a_negative_value_passes_only_when_the_EVIDENCE_is_negative():
    """The rule, stated both ways: the sign belongs to the number. A fact
    whose value really is negative may be quoted with its sign, and the
    positive twin of that fact is then an invented number."""
    assert verify("The level fell by -2.5 m.", ["-2.5"]).ok
    assert not verify("The level fell by 2.5 m.", ["-2.5"]).ok
    assert not verify("The level fell by -2.5 m.", ["2.5"]).ok


def test_numbers_in_reads_a_sign_in_either_script_and_either_minus():
    assert numbers_in("-100.0, −2.5, " + to_devanagari_digits("-7")) == [-100.0, -2.5, -7.0]
