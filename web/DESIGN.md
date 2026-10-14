# Strandline dashboard — design

> Night survey of a Himalayan valley.
> A rescuer opens this at 02:00 with a radio in one hand. Everything on screen has to answer one of
> three questions: where did the flood reach, what did it break, who is cut off.

This file is written before the code and is the contract the code is held to. Section 6 is the
audit against the "generic AI dashboard" tells; anything that failed it was changed here, not later.

---

## 1. The subject, and where the look comes from

Strandline is a **survey instrument**, not an analytics product. The references are:

| Reference | What it gives us |
| --- | --- |
| Topographic survey sheets (1:50,000) | hairline graticule, thin strokes, type set tight and small, labels that sit *on* the terrain |
| Glacial meltwater | the one luminous colour in the palette; everything else is rock and night |
| River silt | the dull, unverified tan of debris — deliberately the least attractive colour on screen |
| Rescue gear and road signs | orange and saffron as *instruction*, condensed sign type, no decoration |
| A night helicopter survey | low light, high contrast, a single moving light source |

What it is **not** taking cues from: tech dashboards, mission-control screens, "command centre"
styling, dark-mode SaaS, data-viz posters.

The product is named after a real thing: the **strandline** is the high-water mark a flood leaves
behind — a line of silt and debris on the valley side. On screen it is exactly that: a thin glowing
line on the outer edge of the flood. It is the one piece of pure craft we spend boldness on.

---

## 2. Tokens

### 2.1 Colour

Semantic names only. A component never names a hue; it names a role.

| Token | Hex | Role |
| --- | --- | --- |
| `--night` | `#1B2430` | map surround, page ground. Never pure black |
| `--panel` | `#222C3A` | panel fill, solid (no glass, no blur) |
| `--ridge` | `#33414F` | the one hairline. All dividers, all borders, 1px |
| `--ridge-soft` | `#2A3543` | inset fills: input wells, sub-rows, the stage rail |
| `--snow` | `#E9EEF2` | primary text, big readouts |
| `--muted` | `#93A1AF` | secondary text, units, captions |
| `--meltwater` | `#6CC6C2` | flood water. High confidence opaque, medium translucent |
| `--silt` | `#B9A887` | possible debris, unverified. Always carries the word "unverified" |
| `--rescue` | `#F2661B` | damaged buildings, things that need action |
| `--saffron` | `#F5B53D` | cut roads, bridges out, warnings |
| `--lichen` | `#8FB98B` | reachable. Used sparingly — at most one element per view |

Derived, no new hues:

- flood high `--meltwater` at 0.92 alpha · flood medium `--meltwater` at 0.45 alpha
- strandline glow: `--meltwater` stroke 1.4px + a 6px blurred duplicate at 0.35 alpha
- low confidence: `--muted` diagonal hatch at 0.22 alpha, 4px pitch, no fill
- cut-off ramp (3 stops only): `#E9EEF2` → `#F5B53D` → `#F2661B`
- focus ring: `--meltwater`, 2px, 2px offset. The same ring everywhere, keyboard and mouse.

**No purple anywhere. No gradient washes. No neon. No pure black. No second accent family.**
Colour budget per view: meltwater + at most two of {rescue, saffron, silt} + lichen once.

### 2.2 Type

Self-hosted via `@fontsource` — the dashboard must come up with no network.

| Family | Use |
| --- | --- |
| Barlow Semi Condensed | headings, numbers, stage labels, buttons. The road-sign voice |
| Barlow | body, chat, captions, long sentences |
| Noto Sans Devanagari | all Nepali, both UI and copilot answers |

Every numeral: `font-variant-numeric: tabular-nums`. Numbers must not jump width when they ease
between runs. **No monospace** — monospace reads as "developer tool", and this is a field instrument.

Scale (1.0 = 16px, 1366×768 is the design width):

| Step | Size / line | Family | Use |
| --- | --- | --- | --- |
| `display` | 34 / 36 | Semi Cond 600 | the one number in a sentence that matters |
| `title` | 20 / 26 | Semi Cond 600 | panel titles, area name |
| `readout` | 17 / 22 | Semi Cond 600, tnum | inline numbers in sentences, card values |
| `body` | 14.5 / 22 | Barlow 400 | sentences, chat |
| `label` | 13 / 18 | Semi Cond 500 | stage names, buttons, legend |
| `caption` | 12 / 17 | Barlow 400 | units, sources, caveats |

Sentence case everywhere. **No ALL-CAPS, no letter-spaced eyebrow labels, no "A · B · C" meta
strings, no arrows in button text, no emoji.** Headings do not get a kicker above them; if a
heading needs explaining, the explanation is a sentence under it.

### 2.3 Space, line, radius, shadow

- Spacing scale: `4 8 12 16 24 32 48`. Panel padding 16; between groups 24; page gutter 16.
- Radius: `3px` on controls, `4px` on panels, `0` on the map and on the stage rail. One family.
- Border: exactly one `1px solid var(--ridge)` hairline. Panels are separated by *edges*, not shadows.
- Shadow: one token, `0 10px 28px rgba(9,13,19,.45)`, and only on an element that genuinely floats
  over the map (panel, popover, bottom sheet). Never on a row, a tile or a button.
- Hit targets ≥ 32px high; ≥ 44px on the phone layout.

### 2.4 Motion

| Thing | Duration / curve |
| --- | --- |
| panel open | 260ms spring (`cubic-bezier(.2,.9,.25,1)`), translate + opacity |
| route draws itself | 700ms, `line-dasharray` march |
| numbers easing between runs | 420ms, ease-out, integer steps |
| layer toggle | 180ms opacity |
| the fly-in | one 4.0s orchestrated sequence, once, on first load |

Motion answers an action. **Nothing animates on hover** except a 1px border lightening and the
cursor. Under `prefers-reduced-motion: reduce`: fly-in skipped (camera starts at the final pose),
intro skipped, routes appear instantly, numbers snap. 60fps or it gets cut.

---

## 3. The memorable moment (the 4-second fly-in)

One sequence, orchestrated, then the screen is calm and stays calm.

```
t=0.0       camera 42 km above the valley, pitch 0, looking straight down, map dim
t=0.0–2.4   the camera falls to 9 km, pitch 0 -> 62 deg, bearing swings onto the valley axis,
            basemap and hillshade fade up. One easeTo with a custom ease, no stepping.
t=1.2–3.0   the flood extent fills DOWNSTREAM: each polygon is keyed by its distance along the
            valley axis and fades in at that offset, so the water runs down the valley.
t=2.6–4.0   the strandline draws itself along the outer edge of the flood, head to tail, as a
            line-gradient wipe; its glow settles to 0.35 alpha and stops.
t=4.0       damage and settlement layers appear with no animation at all. Nothing moves again.
```

Rules: it runs **once** per session, a skip affordance is visible the whole time, and it is skipped
entirely under reduced motion. If terrain tiles have not loaded by t=2.4 the sequence does not wait
— it finishes on the flat map and terrain appears when it is ready. The valley axis (bearing) is
computed from the run's own flood extent (principal axis of the polygon centroids), never
hard-coded to an area or an event.

---

## 4. Layout

Map is full-bleed. Three things sit on it. Nothing else.

```
1366 x 768
+----------------------------------------------------------------------------------+
| +----- mission strip -----+                            +---- ask panel --------+ |
| | 216px wide              |                            | 380px, slides in      | |
| |                         |                            |                       | |
| | Bhote Koshi corridor    |        3D valley,          | Ask about this area   | |
| | 26 August 2026          |        pitch 62 deg,       | ---------------------  | |
| | ----------------------  |        bearing along       | English | Nepali      | |
| | [Check satellite passes]|        the valley          |                       | |
| | [Map this flood]        |                            | how many are cut off? | |
| | ----------------------  |                            | 79 settlements are... | |
| | 1 Satellite passes    * |                            |   ^ click a number,   | |
| | 2 Radar scenes        * |                            |     the camera flies  | |
| | 3 Terrain             o |                            |     to its features   | |
| | 4 Flood extent        . |                            |                       | |
| | 5 Damage              . |                            | How this was answered | |
| | 6 Cut-off             . |                            | [Download sitrep]     | |
| | ----------------------  |                            +-----------------------+ |
| | case_smoke (switcher)   |                                                      |
| +-------------------------+                                      [Evidence]      |
|                                                                  [Ask]           |
| +---- the three answers -------------------------+                               |
| | The flood reached 80 ha of the valley floor.   |  <- click: toggles flood      |
| | It cut 28.5 km of road and 22 bridges.         |  <- click: toggles damage     |
| | 79 settlements lost their route to a clinic.   |  <- click: toggles settlements|
| +------------------------------------------------+                               |
|  Contains modified Copernicus Sentinel data 2026. ... Educational prototype.      |
+----------------------------------------------------------------------------------+
```

- **Mission strip** (left, 216px): area, date, two actions, then the pipeline as a *numbered rail* —
  a real sequence with a vertical line through it, each stage showing state (waiting · running ·
  done · refused). Not six cards. At the foot, the run switcher.
- **The three answers** (bottom left): three plain sentences, each with its numbers set in the
  readout face. Clicking a sentence toggles that sentence's layers; the sentence underlines while
  its layers are on. This is the Track B scorecard, written in words.
- **Ask panel** (right, 380px, closed by default): copilot chat, EN/NE toggle, clickable numbers,
  a collapsed "How this was answered" trace, sitrep download.
- **Settlement card**: appears bottom-centre on click, 320px, four rows, and draws the before/after
  route on the map. Not a modal — the map stays live.
- **Footer**: `strandline.outputs.ATTRIBUTION` verbatim + "Educational prototype — not for
  operational use". 12px, muted, never covered.

### Phone (390x844)

Map keeps the top ~52% of the screen. The mission strip collapses to a single bar with the area,
the date and one primary button. The three answers and the ask panel become **bottom sheets** with
a drag handle, one at a time, at 55% height. The settlement card is a bottom sheet too. Nothing is
horizontally scrollable; nothing is hidden behind hover.

---

## 5. Layer specification

| Layer | Encoding | Label on the legend |
| --- | --- | --- |
| flood, high confidence | `--meltwater` fill 0.92, no stroke | Flood water, high confidence |
| flood, medium confidence | `--meltwater` fill 0.45 | Flood water, medium confidence |
| **strandline** | `--meltwater` line 1.4px + 6px blur at 0.35 | The high-water line |
| low confidence | diagonal hatch, `--muted` 0.22, no fill | Radar cannot see well here |
| possible debris | `--silt` fill 0.5, 1px `--silt` stroke | Possible debris, unverified |
| damaged buildings | fill-extrusion, `--rescue`, height 14 m | Buildings in the flood |
| cut roads | `--saffron` line 2.2px, dash `[2,1.6]` | Roads cut |
| bridges likely out | `--saffron` ring marker with a slash | Bridges likely out, unverified |
| settlements | circle, stroke `--snow` 1px, fill = cut-off ramp | Settlements by cut-off probability |
| routes | before: `--muted` dashed; detour: `--saffron` solid | Route before / after |
| permanent water | `--night` lightened, no stroke | Water before the flood |

Every unverified layer carries the word **unverified** in its legend row, not in a tooltip.
A layer whose endpoint 404s greys its legend row and says "not produced by this run" — it never
throws, and it never silently looks like zero.

---

## 6. Audit against the generic-AI tells

Run before writing a line of code. Each row is a thing the first draft of this plan did, and the fix.

| Tell | Was the plan doing it? | Fix applied in this document |
| --- | --- | --- |
| Identical rounded cards with soft grey shadows | Yes — the first sketch had 4 KPI cards in the strip | Removed. The strip is one panel with a *numbered rail* inside it; stages are rows on a line, not cards. One shadow token, panels only |
| Big-number KPI tiles | Yes — "80 ha / 22 bridges / 79 cut off" as three tiles | Removed. The same numbers now live inside **three sentences** that read as English and double as layer switches. A number is large only *inside* a sentence |
| Frosted glass / backdrop blur | Yes, on the ask panel | Removed. Panels are solid `--panel` with one hairline. The map stays legible because the panel is opaque |
| Gradient washes | Yes, a vignette behind the strip | Removed. MapLibre's own atmospheric `fog` does that job on the terrain itself, which is honest depth rather than painted-on |
| Hover animation on everything | Yes (lift + glow on buttons and rows) | Cut to: the border lightens, the cursor changes. Motion is reserved for actions |
| Neon / purple accents | The palette had a violet for "selected" | Replaced: selection is a `--snow` 1px ring plus the 2px `--meltwater` focus ring |
| ALL-CAPS eyebrow labels | Yes — "MISSION", "LAYERS" | Removed. Panels have sentence-case titles or no title at all |
| "A · B · C" meta strings | Yes — "orbit 85 · ascending · 2 d latency" | Rewritten as a sentence: "Ascending orbit 85, two days after the flood." |
| Emoji / arrows in buttons | Yes — "Map this flood →" | Removed. Verb only |
| Monospace for numbers | Yes | Replaced with Barlow Semi Condensed + `tabular-nums` |
| Every panel the same weight | Yes, three equal panels | Hierarchy: the three sentences are the loudest thing after the map; the strip is quiet; the ask panel is closed until asked for |
| A chart for its own sake | A sparkline of flood area by confidence | Cut. Two numbers in a sentence beat a two-bar chart |
| Decorative map "scan line" overlays | Considered | Cut. The only animated map element is the strandline, once |

### What we spend boldness on (one thing)

The fly-in and the strandline drawing itself. Everything else is deliberately quiet so that moment
reads. If a later addition competes with it, the addition loses.

### Honesty constraints that shape the UI (Rules 1, 3, 6)

- Every number on screen comes from `run_report.json` or a layer file served by the API. The web
  app does **no** arithmetic beyond percentages whose two inputs it can also show, and each of the
  three sentences names its source file.
- The pipeline does not produce a flooded **river length**, so sentence 1 reports flood extent in
  hectares and the high/medium confidence split — never an invented length. (Logged in
  `docs/open_items.md`.)
- "Possible debris, unverified", "likely out, unverified" and "cut-off probability is a share of a
  perturbed ensemble, a sensitivity measure, not a calibrated chance" are printed in the UI, not
  buried in a tooltip.
- A refused or failed run gets a plain directive message: what happened, why, what to try. Never a
  partial map presented as a finished one.
