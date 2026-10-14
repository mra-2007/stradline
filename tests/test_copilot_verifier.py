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
