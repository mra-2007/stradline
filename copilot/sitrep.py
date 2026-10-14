"""The one-page situation report, in English and Nepali.

    venv/Scripts/python.exe -m copilot.sitrep --run runs/<name> --lang en|ne --out FILE.pdf

Rendered as HTML and PRINTED by a headless browser (Chrome or Edge), not by a
PDF library. That is not a preference: Devanagari needs real text shaping -
`क` + `्` + `ष` has to become the conjunct क्ष, and vowel signs have to
reorder around the consonant they attach to. ReportLab, FPDF and friends draw
the codepoints one after another and produce text a Nepali reader cannot
read. A browser has HarfBuzz behind it and gets it right, and the font is
embedded as a data URI so the output does not depend on what is installed on
the machine that opens the PDF.

EVERY NUMBER ON THE PAGE IS A FACT. The page is built from the same
`EvidenceStore` the copilot answers from, through the same formatter, and
`numbers_on_page` returns every value it printed with its fact id - which is
what the test uses to prove the page invented nothing.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from strandline.config import (
    COPILOT_LANGUAGES,
    CUTOFF_GATE_GRAPH,
    CUTOFF_GATE_SCENARIO,
    CUTOFF_GATE_TARGET,
    SitrepParams,
    STATUS_CUT_OFF,
)
from strandline.outputs import ATTRIBUTION
from copilot.evidence import EvidenceStore
from copilot.executor import load_store
from copilot.templates import load_i18n

ROOT = Path(__file__).resolve().parents[1]

# Where a headless Chromium lives on Windows, in the order worth trying. A
# `playwright` install is preferred when it is there; these are the fallback
# the brief asks for, and one of them is on every Windows machine.
BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

# The key numbers table: (i18n label key, fact id). A fact that is not in the
# run is skipped, never shown as a zero.
KEY_NUMBERS = (
    ("flood_extent_ha", "flood.extent_ha"),
    ("flood_high_ha", "flood.high_ha"),
    ("flood_medium_ha", "flood.medium_ha"),
    ("flood_debris_ha", "flood.debris_ha"),
    ("flood_permanent_water_ha", "flood.permanent_water_ha"),
    ("flood_not_analysed_pct", "flood.not_analysed_pct"),
    ("damage_buildings_damaged", "damage.buildings_damaged"),
    ("damage_roads_segments_cut", "damage.roads_segments_cut"),
    ("damage_roads_length_cut_km", "damage.roads_length_cut_km"),
    ("damage_bridges_likely_out", "damage.bridges_likely_out"),
    ("cutoff_cut_off_count", "cutoff.cut_off_count"),
    ("cutoff_settlements_total", "cutoff.settlements_total"),
)


class SitrepError(RuntimeError):
    """The report could not be produced, with a message saying what to try."""


@dataclass
class Sitrep:
    html: str
    numbers: list = field(default_factory=list)      # [{fact_id, formatted, unit}]
    lang: str = "en"


def find_browser(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    for name in ("chrome", "chromium", "msedge", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    try:                                             # pragma: no cover - optional
        from playwright.sync_api import sync_playwright  # noqa: F401
        return "playwright"
    except ImportError:
        return None


def font_data_uri(params: SitrepParams) -> str | None:
    path = ROOT / params.devanagari_font
    if not path.exists():
        return None
    return ("data:font/ttf;base64,"
            + base64.b64encode(path.read_bytes()).decode("ascii"))


def _image_data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _top_cutoff(store: EvidenceStore, limit: int) -> list:
    rows = []
    for i in range(store.settlement_count()):
        status = store.get(f"settlement.{i}.status")
        probability = store.get(f"settlement.{i}.cut_off_probability")
        name = store.get(f"settlement.{i}.name")
        if status is None or name is None or status.value != STATUS_CUT_OFF:
            continue
        rows.append((probability.value if probability else 0.0, i, name, status, probability))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return rows[:limit]


CSS = """
@page {{ size: {page_size}; margin: {margin}in; }}
{font_face}
:root {{ --ink: #11161c; --muted: #5a6572; --rule: #c9d1d9; --accent: #0b4f8a;
         --warn: #8a3b0b; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: var(--ink); background: #fff;
        font-family: {font_stack}; font-size: 9.2pt; line-height: 1.38; }}
h1 {{ font-size: 16pt; margin: 0 0 1mm; letter-spacing: -0.2px; }}
h2 {{ font-size: 9.6pt; margin: 3.5mm 0 1.2mm; text-transform: none;
      color: var(--accent); border-bottom: 1px solid var(--rule);
      padding-bottom: 0.7mm; }}
.sub {{ color: var(--muted); font-size: 8.4pt; margin: 0 0 1mm; }}
.prototype {{ display: inline-block; border: 1px solid var(--warn); color: var(--warn);
              padding: 0.6mm 1.6mm; font-size: 7.6pt; border-radius: 2px; }}
.head {{ display: flex; justify-content: space-between; align-items: flex-start;
         gap: 6mm; border-bottom: 2px solid var(--accent); padding-bottom: 2mm; }}
.meta {{ text-align: right; color: var(--muted); font-size: 7.8pt; white-space: nowrap; }}
.cols {{ display: flex; gap: 6mm; margin-top: 2mm; }}
.col {{ flex: 1 1 0; min-width: 0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 8.6pt; }}
th {{ text-align: left; color: var(--muted); font-weight: 600; font-size: 7.8pt;
      border-bottom: 1px solid var(--rule); padding: 0.8mm 1mm; }}
td {{ padding: 0.8mm 1mm; border-bottom: 1px solid #eceff2; vertical-align: top; }}
td.n {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
td.fid {{ color: var(--muted); font-size: 7pt; word-break: break-all; }}
img.map {{ width: 100%; border: 1px solid var(--rule); border-radius: 2px; }}
.box {{ border: 1px solid var(--rule); border-left: 3px solid var(--warn);
        padding: 1.6mm 2mm; font-size: 8pt; color: var(--ink); border-radius: 2px; }}
.attrib {{ color: var(--muted); font-size: 6.9pt; margin-top: 3mm;
           border-top: 1px solid var(--rule); padding-top: 1.2mm; }}
p {{ margin: 0 0 1.4mm; }}
"""

# Declared TWICE, at 400 and at 700, both pointing at the same single-weight
# file. With one face Chrome preferred a system font that HAS a real bold for
# the bold runs, and the page came out in two different typefaces; this makes
# it synthesise the bold from the embedded file instead.
FONT_FACE = """
@font-face {{ font-family: "{family}"; src: url("{uri}") format("truetype");
              font-weight: 400; font-style: normal; font-display: block; }}
@font-face {{ font-family: "{family}"; src: url("{uri}") format("truetype");
              font-weight: 700; font-style: normal; font-display: block; }}
"""

PAGE = """<meta charset="utf-8"><title>{title}</title><style>{css}</style>
<div class="head">
  <div>
    <h1>{title}</h1>
    <p class="sub">{subtitle}</p>
    <span class="prototype">{prototype}</span>
  </div>
  <div class="meta">
    {event_label}: <b>{event_date}</b><br>
    {sensor_label}: <b>{sensor}</b><br>
    {area_label}: {bbox}<br>
    {run_label}: {run_id} &middot; {hash_label}: {params_hash}
  </div>
</div>
<div class="cols">
  <div class="col">
    <h2>{key_numbers_label}</h2>
    <table><thead><tr><th>{metric_label}</th><th class="n">{value_label}</th>
      <th>{source_label}</th></tr></thead><tbody>{key_rows}</tbody></table>
    <h2>{infrastructure_label}</h2>
    {infrastructure}
  </div>
  <div class="col">
    <h2>{map_label}</h2>
    {map_html}
    <h2>{cutoff_label}</h2>
    <table><thead><tr><th>{settlement_label}</th><th class="n">{probability_label}</th>
      <th>{status_label}</th></tr></thead><tbody>{cutoff_rows}</tbody></table>
  </div>
</div>
<h2>{confidence_label}</h2>
<p>{confidence}</p>
<h2>{limitations_label}</h2>
<div class="box">{limitations}</div>
<div class="attrib"><b>{attribution_label}:</b> {attribution}<br>{generated}</div>
"""


def build_html(store: EvidenceStore, lang: str = "en",
               params: SitrepParams | None = None) -> Sitrep:
    """The page, and the list of every number printed on it."""
    params = params or SitrepParams()
    i18n = load_i18n(lang)
    labels = i18n["labels"]
    numbers: list = []

    def value_of(fact_id: str) -> str:
        fact = store.get(fact_id)
        if fact is None:
            return labels["not_recorded"]
        text = fact.formatted(lang)
        numbers.append({"fact_id": fact.id, "formatted": text, "unit": fact.unit,
                        "value": fact.value})
        return text

    key_rows = []
    for label_key, fact_id in KEY_NUMBERS:
        fact = store.get(fact_id)
        if fact is None or fact.value is None:
            continue
        key_rows.append(
            f"<tr><td>{_esc(labels[label_key])}</td>"
            f"<td class='n'>{_esc(value_of(fact_id))}</td>"
            f"<td class='fid'>{_esc(fact_id)}</td></tr>")

    cutoff_rows = []
    for _, _i, name, status, probability in _top_cutoff(store,
                                                        params.top_cutoff_settlements):
        numbers.append({"fact_id": name.id, "formatted": name.formatted(lang),
                        "unit": name.unit, "value": name.value})
        probability_text = value_of(probability.id) if probability is not None \
            else labels["not_recorded"]
        cutoff_rows.append(
            f"<tr><td>{_esc(name.formatted(lang))}</td>"
            f"<td class='n'>{_esc(probability_text)}</td>"
            f"<td>{_esc(status.formatted(lang))}</td></tr>")
    if not cutoff_rows:
        reason = store.missing.get("cutoff") or labels["none"]
        cutoff_rows.append(f"<tr><td colspan='3'>{_esc(reason)}</td></tr>")

    infrastructure = _infrastructure(store, i18n, lang, value_of)
    confidence = _confidence(store, i18n, lang, value_of)
    limitations = _limitations(store, i18n, lang, value_of)

    sensor_bits = [value_of("sensor.sensors_used")]
    if store.get("sensor.post_pass_date") is not None:
        sensor_bits.append(value_of("sensor.post_pass_date"))
    uri = font_data_uri(params)
    css = CSS.format(
        page_size=params.page_size, margin=params.margin_in,
        font_face=FONT_FACE.format(family=params.devanagari_font_family, uri=uri)
        if uri else "",
        font_stack=(f'"{params.devanagari_font_family}", "Nirmala UI", '
                    f'"Segoe UI", system-ui, sans-serif'))

    page = PAGE.format(
        css=css, title=_esc(labels["sitrep_title"]),
        subtitle=_esc(labels["sitrep_subtitle"]),
        prototype=_esc(labels["sitrep_prototype"]),
        event_label=_esc(labels["sitrep_event_date"]),
        event_date=_esc(value_of("run.event_date")),
        sensor_label=_esc(labels["sitrep_sensor"]), sensor=_esc(", ".join(sensor_bits)),
        area_label=_esc(labels["sitrep_area"]), bbox=_esc(value_of("run.bbox_text")),
        run_label=_esc(labels["sitrep_run"]), run_id=_esc(value_of("run.id")),
        hash_label=_esc(labels["sitrep_params_hash"]),
        params_hash=_esc(value_of("run.params_hash")),
        key_numbers_label=_esc(labels["sitrep_key_numbers"]),
        metric_label=_esc(labels["sitrep_metric"]),
        value_label=_esc(labels["sitrep_value"]),
        source_label=_esc(labels["sitrep_source"]), key_rows="".join(key_rows),
        infrastructure_label=_esc(labels["sitrep_infrastructure"]),
        infrastructure=infrastructure,
        map_label=_esc(labels["sitrep_map"]), map_html=_map_html(store, labels),
        cutoff_label=_esc(labels["sitrep_cutoff"]),
        settlement_label=_esc(labels["sitrep_settlement"]),
        probability_label=_esc(labels["sitrep_probability"]),
        status_label=_esc(labels["sitrep_status"]), cutoff_rows="".join(cutoff_rows),
        confidence_label=_esc(labels["sitrep_confidence"]), confidence=confidence,
        limitations_label=_esc(labels["sitrep_limitations"]), limitations=limitations,
        attribution_label=_esc(labels["sitrep_attribution"]),
        attribution=_esc(" ".join(ATTRIBUTION)),
        generated=_esc(labels["sitrep_generated"]))
    return Sitrep(html=page, numbers=numbers, lang=lang)


def _map_html(store: EvidenceStore, labels) -> str:
    uri = _image_data_uri(store.run_dir / "quicklook.png")
    if uri is None:
        return f"<p class='sub'>{_esc(labels['sitrep_no_map'])}</p>"
    return f"<img class='map' src='{uri}' alt='{_esc(labels[chr(115) + 'itrep_map'])}'>"


def _sentence(store, i18n, lang, value_of, template_key: str, pairs) -> str:
    """Fill one bundled template from fact ids, skipping it when the run does
    not have what it needs. Same templates the copilot answers with, so the
    page and the answers cannot word the same fact differently."""
    slots = {}
    for name, fact_id in pairs:
        fact = store.get(fact_id)
        if fact is None:
            return ""
        slots[name] = value_of(fact_id) if fact.unit not in ("text", "status", "id") \
            else _plain(store, fact, lang, value_of)
    return _esc(i18n["templates"][template_key].format(**slots))


def _plain(store, fact, lang, value_of) -> str:
    value_of(fact.id)
    return fact.formatted(lang)


def _infrastructure(store: EvidenceStore, i18n, lang, value_of) -> str:
    if "damage" in store.missing:
        return f"<p class='sub'>{_esc(store.missing['damage'])}</p>"
    parts = []
    for key, pairs in (
        ("damaged_buildings", (("buildings_damaged", "damage.buildings_damaged"),
                               ("buildings_total", "damage.buildings_total"),
                               ("buildings_damaged_pct", "damage.buildings_damaged_pct"))),
    ):
        text = _sentence(store, i18n, lang, value_of, key, pairs)
        if text:
            parts.append(f"<p>{text}</p>")
    roads = []
    for label_key, fact_id in (("damage_roads_segments_cut", "damage.roads_segments_cut"),
                               ("damage_roads_length_cut_km", "damage.roads_length_cut_km"),
                               ("damage_bridges_likely_out", "damage.bridges_likely_out")):
        if store.get(fact_id) is None:
            continue
        roads.append(f"<tr><td>{_esc(i18n['labels'][label_key])}</td>"
                     f"<td class='n'>{_esc(value_of(fact_id))}</td>"
                     f"<td class='fid'>{_esc(fact_id)}</td></tr>")
    if roads:
        parts.append("<table><tbody>" + "".join(roads) + "</tbody></table>")
    bridge_label = store.get("damage.bridge_status_label")
    if bridge_label is not None:
        parts.append(f"<p class='sub'>{_esc(bridge_label.formatted(lang))}</p>")
    return "".join(parts) or f"<p class='sub'>{_esc(i18n['labels']['none'])}</p>"


def _confidence(store: EvidenceStore, i18n, lang, value_of) -> str:
    bits = []
    if store.get("flood.high_share_pct") is not None:
        bits.append(_sentence(store, i18n, lang, value_of, "flooded_area",
                              (("extent_ha", "flood.extent_ha"),
                               ("high_ha", "flood.high_ha"),
                               ("medium_ha", "flood.medium_ha"),
                               ("high_share_pct", "flood.high_share_pct"),
                               ("permanent_water_ha", "flood.permanent_water_ha"),
                               ("analysed_area_ha", "flood.analysed_area_ha"),
                               ("extent_share_of_analysed_pct",
                                "flood.extent_share_of_analysed_pct"))))
    debris = store.get("flood.debris_label")
    if debris is not None and store.get("flood.debris_ha") is not None:
        bits.append(f"{_esc(i18n['labels']['flood_debris_ha'])}: "
                    f"{_esc(value_of('flood.debris_ha'))}")
    cutoff_caveat = store.get("cutoff.probability_meaning")
    if cutoff_caveat is not None:
        bits.append(_esc(cutoff_caveat.formatted(lang)))
    return " ".join(b for b in bits if b)


def _limitations(store: EvidenceStore, i18n, lang, value_of) -> str:
    """The limitations box, built from the copilot's own limitations tool so
    the page and an answer cannot disagree about what this map cannot say."""
    from copilot.tools import limitations as limitations_tool
    from copilot.templates import fill

    result = limitations_tool(store, lang, i18n)
    for slot in result.slots.values():
        if slot.fact is not None:
            value_of(slot.fact.id)
    text = fill(i18n["templates"][result.template_key], result.slots)
    gated = store.get("cutoff.gated_definition")
    if gated is not None:
        text += " " + str(gated.value) + "."
    return _esc(text)


def numbers_on_page(sitrep: Sitrep) -> list:
    """Every value the page printed, with the fact it came from."""
    return list(sitrep.numbers)


def verifiable_text(text: str) -> str:
    """The part of the page whose numbers are CLAIMS about this event.

    Everything up to the attribution footer. The footer is reproduced
    verbatim because Rule 1 requires it, and its digits are the years of a
    copyright notice - "Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014" - not
    measurements of anything. Cutting it here, by name, keeps the number
    check over the rest of the page absolutely strict instead of having to
    admit a handful of bare years into the evidence, where they could then
    excuse a wrong hectare figure.
    """
    cleaned = re.sub(r"<style>.*?</style>", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"data:[^\s\"']+", " ", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    for line in ATTRIBUTION:
        head = line.split(" ")[0] + " " + " ".join(line.split(" ")[1:4])
        index = cleaned.find(head)
        if index != -1:
            cleaned = cleaned[:index]
    return cleaned


# --- printing ---------------------------------------------------------------


def print_pdf(html_text: str, out: Path, params: SitrepParams | None = None,
              browser: str | None = None) -> Path:
    """HTML in, PDF out, through a headless browser."""
    params = params or SitrepParams()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    found = find_browser(browser)
    if found is None:
        raise SitrepError(
            "No headless browser was found, and the situation report needs one: a PDF "
            "library cannot shape Devanagari conjuncts correctly. Install Google Chrome "
            "or Microsoft Edge, or `pip install playwright && playwright install "
            "chromium`, then run this command again.")
    if found == "playwright":                        # pragma: no cover - optional
        return _print_with_playwright(html_text, out, params)

    with tempfile.TemporaryDirectory(prefix="strandline-sitrep-") as tmp:
        source = Path(tmp) / "sitrep.html"
        source.write_text(html_text, encoding="utf-8")
        command = [found, "--headless=new", "--disable-gpu", "--no-sandbox",
                   "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
                   "--virtual-time-budget=10000",
                   f"--user-data-dir={Path(tmp) / 'profile'}",
                   f"--print-to-pdf={out.resolve()}", source.resolve().as_uri()]
        try:
            finished = subprocess.run(command, capture_output=True, text=True,
                                      timeout=params.browser_timeout_s)
        except subprocess.TimeoutExpired as err:
            raise SitrepError(
                f"{Path(found).name} did not finish printing within "
                f"{params.browser_timeout_s:.0f} s. Try again, or pass --browser with a "
                f"different Chromium build.") from err
        if not out.exists():
            raise SitrepError(
                f"{Path(found).name} exited with code {finished.returncode} and wrote no "
                f"PDF.\nstderr: {finished.stderr[-600:]}")
    return out


def _print_with_playwright(html_text: str, out: Path,
                           params: SitrepParams) -> Path:   # pragma: no cover
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page()
        page.set_content(html_text, wait_until="load")
        page.pdf(path=str(out), format=params.page_size,
                 margin={side: f"{params.margin_in}in"
                         for side in ("top", "bottom", "left", "right")},
                 print_background=True)
        browser.close()
    return out


def build(run_dir, lang: str = "en", out: Path | None = None,
          params: SitrepParams | None = None, browser: str | None = None):
    """Evidence -> HTML -> PDF. Returns (path, Sitrep)."""
    store = load_store(run_dir)
    sitrep = build_html(store, lang, params)
    if out is None:
        return None, sitrep
    return print_pdf(sitrep.html, Path(out), params, browser), sitrep


def main(argv=None) -> int:
    from copilot.ask import _utf8_stdout

    _utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--lang", default="en", choices=list(COPILOT_LANGUAGES))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--browser", default=None, help="path to a Chromium build")
    ap.add_argument("--html", action="store_true", help="also write the HTML beside it")
    ap.add_argument("--numbers", action="store_true",
                    help="print every number on the page with its fact id")
    args = ap.parse_args(argv)

    try:
        path, sitrep = build(args.run, args.lang, args.out, browser=args.browser)
    except SitrepError as err:
        print(str(err), file=sys.stderr)
        return 2
    if args.html:
        Path(args.out).with_suffix(".html").write_text(sitrep.html, encoding="utf-8")
    if args.numbers:
        print(json.dumps(numbers_on_page(sitrep), ensure_ascii=False, indent=1))
    print(f"wrote {path} ({args.lang}), {len(sitrep.numbers)} number(s), every one from "
          f"this run's evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
