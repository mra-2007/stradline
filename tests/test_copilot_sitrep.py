"""The situation report: only evidence numbers on the page, and a Nepali PDF
whose conjunct letters are actually shaped."""

import re

import pytest

from strandline.config import SitrepParams
from strandline.outputs import ATTRIBUTION
from copilot.evidence import EvidenceStore
from copilot.sitrep import (build, build_html, find_browser, font_data_uri,
                            numbers_on_page, print_pdf, verifiable_text)
from copilot.units import NUMBER_RE, to_ascii_digits, to_devanagari_digits
from copilot.verifier import verify
from tests.conftest_copilot import write_run

pymupdf = pytest.importorskip("pymupdf")

# A word with a conjunct (क्ष / स्थ / त्र) and a word with a PREPOSED vowel
# sign. Both are in the Nepali page's own text, so the check is on the real
# report rather than on a string invented for the test.
CONJUNCT_WORD = "स्थिति"


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return EvidenceStore.from_run(write_run(tmp_path_factory.mktemp("run") / "synthetic"))


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    return write_run(tmp_path_factory.mktemp("run2") / "synthetic")


# --- the page content -------------------------------------------------------

def test_every_number_on_the_page_is_an_evidence_number(store):
    """The claim the whole page rests on. Strip the HTML, find every number,
    and check each one against the evidence - the same check the copilot's
    answers go through."""
    page = build_html(store, "en")
    result = verify(verifiable_text(page.html), store.allowed_strings("en"), "en")
    assert result.ok, [f.as_dict() for f in result.findings]


def test_every_number_on_the_nepali_page_is_an_evidence_number(store):
    page = build_html(store, "ne")
    result = verify(verifiable_text(page.html), store.allowed_strings("ne"), "ne")
    assert result.ok, [f.as_dict() for f in result.findings]


def test_the_page_reports_which_fact_each_number_came_from(store):
    page = build_html(store, "en")
    numbers = numbers_on_page(page)
    assert numbers
    for number in numbers:
        assert number["fact_id"] in store.facts
        assert store.get(number["fact_id"]).formatted("en") == number["formatted"]


def test_the_page_carries_the_required_attribution_and_the_prototype_label(store):
    page = build_html(store, "en").html
    for line in ATTRIBUTION:
        assert line in page
    assert "Educational prototype" in page
    assert "educational prototype" in page.lower()


def test_the_page_carries_the_run_id_and_params_hash(store):
    page = build_html(store, "en").html
    assert "0123456789ab" in page
    assert "synthetic" in page


def test_the_page_names_the_sensor_and_the_pass_date(store):
    page = build_html(store, "en").html
    assert "2024-07-18" in page and "Sentinel-1 and Sentinel-2" in page


def test_the_page_lists_the_cut_off_settlements_with_their_probability(store):
    page = build_html(store, "en").html
    assert "Khanigaun" in page and "0.92" in page


def test_a_run_without_osm_says_so_instead_of_showing_zero(tmp_path):
    store = EvidenceStore.from_run(write_run(tmp_path / "no_osm", with_damage=False,
                                             with_cutoff=False))
    page = build_html(store, "en").html
    assert "ohsome could not be reached" in page
    assert "Buildings inside the flood extent" not in page


def test_the_limitations_box_uses_the_copilot_wording(store):
    page = build_html(store, "en").html
    assert "educational prototype" in page.lower()
    assert "OpenStreetMap snapshot dated 2024-07-14" in page


def test_the_font_is_embedded_in_the_html(store):
    uri = font_data_uri(SitrepParams())
    assert uri and uri.startswith("data:font/ttf;base64,")
    assert uri in build_html(store, "ne").html


# --- the PDF ----------------------------------------------------------------

browser_needed = pytest.mark.skipif(
    find_browser() is None,
    reason="no headless Chromium on this machine; the sitrep needs one to shape "
           "Devanagari correctly")


@browser_needed
def test_the_english_pdf_is_one_page(run_dir, tmp_path):
    path, _ = build(run_dir, "en", tmp_path / "en.pdf")
    with pymupdf.open(path) as doc:
        assert doc.page_count == 1


@browser_needed
def test_the_nepali_pdf_embeds_noto_and_uses_it_for_every_devanagari_run(run_dir,
                                                                        tmp_path):
    path, _ = build(run_dir, "ne", tmp_path / "ne.pdf")
    with pymupdf.open(path) as doc:
        page = doc[0]
        assert doc.page_count == 1
        fonts = {name.split("+")[-1] for *_x, name, _y, _z in
                 ((f[0], f[1], f[2], f[3], f[4], f[5]) for f in page.get_fonts())}
        assert any("NotoSansDevanagari" in name for name in fonts), fonts
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if re.search(r"[ऀ-ॿ]", span["text"]):
                        assert "NotoSansDevanagari" in span["font"], span


@browser_needed
def test_the_nepali_pdf_shapes_conjuncts(run_dir, tmp_path):
    """Devanagari is only readable if the renderer formed conjuncts and
    reordered the vowel signs. A PDF library that drew the codepoints one
    after another would produce text of EXACTLY the unshaped advance width;
    a shaped run is measurably narrower, because several codepoints became
    one glyph.
    """
    path, _ = build(run_dir, "ne", tmp_path / "ne.pdf")
    font = pymupdf.Font(fontfile=str(SitrepParams().devanagari_font))
    checked = 0
    with pymupdf.open(path) as doc:
        for block in doc[0].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if CONJUNCT_WORD not in span["text"]:
                        continue
                    drawn = span["bbox"][2] - span["bbox"][0]
                    unshaped = font.text_length(span["text"], fontsize=span["size"])
                    assert drawn < unshaped * 0.95, (
                        f"{span['text']!r} drawn at {drawn:.1f} vs unshaped "
                        f"{unshaped:.1f}: the conjuncts were not formed")
                    checked += 1
    assert checked, f"{CONJUNCT_WORD!r} is not on the Nepali page any more"


@browser_needed
def test_the_nepali_pdf_text_reads_back_as_the_same_devanagari(run_dir, tmp_path):
    """Shaping must not cost the TEXT: a reader copying a number out of the
    PDF has to get the number the evidence holds."""
    path, sitrep = build(run_dir, "ne", tmp_path / "ne.pdf")
    with pymupdf.open(path) as doc:
        text = "".join(page.get_text() for page in doc)
    assert to_devanagari_digits("100.0") in text
    for number in sitrep.numbers:
        if number["unit"] in ("ha", "count") and number["formatted"] != "—":
            assert number["formatted"] in text, number


@browser_needed
def test_no_number_in_the_printed_pdf_is_outside_the_evidence(run_dir, tmp_path, store):
    path, sitrep = build(run_dir, "en", tmp_path / "en.pdf")
    with pymupdf.open(path) as doc:
        text = "".join(page.get_text() for page in doc)
    result = verify(verifiable_text(text), store.allowed_strings("en"), "en")
    assert result.ok, [f.as_dict() for f in result.findings]


def test_a_missing_browser_gives_an_actionable_message(monkeypatch, tmp_path):
    import copilot.sitrep as sitrep_module
    from copilot.sitrep import SitrepError

    monkeypatch.setattr(sitrep_module, "find_browser", lambda *_a, **_k: None)
    with pytest.raises(SitrepError) as caught:
        print_pdf("<p>x</p>", tmp_path / "x.pdf")
    assert "Chrome" in str(caught.value) and "playwright" in str(caught.value)


def test_digits_round_trip_between_scripts():
    assert to_ascii_digits(to_devanagari_digits("1234.5")) == "1234.5"
    assert NUMBER_RE.findall(to_devanagari_digits("12.5")) == [to_devanagari_digits("12.5")]
