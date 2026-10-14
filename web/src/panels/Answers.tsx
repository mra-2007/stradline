/* The three sentences that answer Track B's three questions.

   They are the loudest thing on screen after the map, they read as English,
   and each one is also the switch for its own layers. The layer swatches
   appear under the sentence that is on, which is where the legend lives. */

import type { Sentence, SentenceKey } from "../lib/answers";
import type { LayerName, LoadedLayer } from "../lib/api";
import { C } from "../map/style";

interface Props {
  sentences: Sentence[];
  layers: Partial<Record<LayerName, LoadedLayer>>;
  visibleGroups: Record<"reached" | "broke" | "stranded", boolean>;
  showLowConfidence: boolean;
  onToggleGroup(key: SentenceKey): void;
  onToggleLowConfidence(): void;
  loading: boolean;
}

interface Swatch {
  label: string;
  colour: string;
  shape: "fill" | "line" | "dash" | "ring" | "hatch" | "ramp";
  /** The layer whose absence greys this row out. */
  layer?: LayerName;
}

const SWATCHES: Record<SentenceKey, Swatch[]> = {
  reached: [
    { label: "Flood water, high confidence", colour: C.meltwater, shape: "fill", layer: "flood" },
    { label: "Flood water, medium confidence", colour: "rgba(108,198,194,.45)", shape: "fill", layer: "flood" },
    { label: "The strandline: the flood's high-water line", colour: "#A8E6E3", shape: "line", layer: "flood" },
    { label: "Possible debris, unverified", colour: C.silt, shape: "fill", layer: "flood" },
    { label: "Radar cannot see well here", colour: C.muted, shape: "hatch", layer: "low_confidence" },
  ],
  broke: [
    { label: "Buildings inside the flood", colour: C.rescue, shape: "fill", layer: "damaged_buildings" },
    { label: "Roads cut", colour: C.saffron, shape: "dash", layer: "cut_roads" },
    { label: "Bridges likely out, unverified", colour: C.saffron, shape: "ring", layer: "bridges_out" },
  ],
  stranded: [
    { label: "Settlements by cut-off probability", colour: "", shape: "ramp", layer: "settlements" },
    { label: "Route link of the settlement you pick", colour: C.muted, shape: "dash", layer: "routes" },
  ],
};

function SwatchMark({ swatch }: { swatch: Swatch }) {
  if (swatch.shape === "ramp") {
    return (
      <span className="swatch swatch--ramp" aria-hidden="true">
        <i style={{ background: C.snow }} />
        <i style={{ background: C.saffron }} />
        <i style={{ background: C.rescue }} />
      </span>
    );
  }
  return (
    <span
      className={`swatch swatch--${swatch.shape}`}
      style={{ "--swatch": swatch.colour } as React.CSSProperties}
      aria-hidden="true"
    />
  );
}

export function Answers(props: Props) {
  const { sentences, layers, visibleGroups, loading } = props;

  return (
    <section className="answers panel" aria-label="What this run found">
      {loading && sentences.every((s) => s.pieces.length === 0) && (
        <p className="answers__loading caption">Reading the run…</p>
      )}

      {sentences.map((sentence) => {
        const on = visibleGroups[sentence.key];
        return (
          <article key={sentence.key} className={`answer${on ? " answer--on" : ""}`}>
            <button
              type="button"
              className="answer__sentence"
              onClick={() => props.onToggleGroup(sentence.key)}
              aria-pressed={on}
              title={
                sentence.sources.length > 0
                  ? `Every number here comes from ${sentence.sources.join(" and ")}`
                  : undefined
              }
            >
              {sentence.unavailable ? (
                <span className="answer__unavailable">{sentence.unavailable}</span>
              ) : (
                <span className="answer__text">
                  {sentence.pieces.map((piece, index) =>
                    piece.value !== undefined ? (
                      <span className="answer__figure" key={index} title={piece.source}>
                        <span className="readout">{piece.value}</span>
                        {piece.unit ? <span className="answer__unit"> {piece.unit}</span> : null}
                      </span>
                    ) : (
                      <span key={index}>{piece.text} </span>
                    ),
                  )}
                </span>
              )}
            </button>

            {sentence.caveat && <p className="answer__caveat caption">{sentence.caveat}</p>}

            {on && (
              <ul className="legend">
                {SWATCHES[sentence.key].map((swatch) => {
                  const layer = swatch.layer ? layers[swatch.layer] : undefined;
                  const missing = swatch.layer ? (layer?.missing ?? null) : null;
                  const isLowConf = swatch.layer === "low_confidence";
                  return (
                    <li
                      key={swatch.label}
                      className={`legend__row${missing ? " legend__row--missing" : ""}`}
                    >
                      {isLowConf && !missing ? (
                        <label className="legend__toggle">
                          <input
                            type="checkbox"
                            checked={props.showLowConfidence}
                            onChange={props.onToggleLowConfidence}
                          />
                          <SwatchMark swatch={swatch} />
                          <span>{swatch.label}</span>
                        </label>
                      ) : (
                        <>
                          <SwatchMark swatch={swatch} />
                          <span>{swatch.label}</span>
                        </>
                      )}
                      {missing && <span className="legend__missing caption">{missing}</span>}
                    </li>
                  );
                })}
              </ul>
            )}
          </article>
        );
      })}
    </section>
  );
}
