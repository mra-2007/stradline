/* The three sentences that answer Track B's three questions, built from the
   run's own report and layer files and nothing else.

   Rules this file exists to keep (CLAUDE.md Rules 3 and 6):
   - every number is read out of run_report.json, or is a property value read
     verbatim from a layer the API served. `source` says which, and the UI
     prints it.
   - no arithmetic beyond a percentage whose two inputs are also on screen.
   - when the pipeline did not produce something, the sentence says so in the
     pipeline's own words instead of showing a zero.
*/

import type { LayerName, LoadedLayer, RunReport } from "./api";
import { measure, num, percent } from "./format";

export type SentenceKey = "reached" | "broke" | "stranded";

export interface Piece {
  /** Plain words. */
  text?: string;
  /** A number, set in the readout face. */
  value?: string;
  unit?: string;
  /** Where this number came from, shown on hover and in the source line. */
  source?: string;
}

export interface Sentence {
  key: SentenceKey;
  question: string;
  pieces: Piece[];
  /** Layers this sentence switches on. */
  layers: LayerName[];
  /** Caveat printed under the sentence — never hidden in a tooltip. */
  caveat?: string;
  /** Set when the pipeline did not produce this answer. */
  unavailable?: string;
  sources: string[];
}

const REPORT = "run_report.json";

function get(report: RunReport | null, path: string): any {
  if (!report) return undefined;
  return path.split(".").reduce<any>((node, key) => (node == null ? undefined : node[key]), report);
}

function flood(report: RunReport | null): Sentence {
  const extentHa = get(report, "flood_extent.flood_extent_ha");
  const areas = get(report, "areas_ha") ?? {};
  const high = areas["flood water (high confidence)"];
  const medium = areas["flood water (medium confidence)"];
  const narrowFraction = get(
    report,
    "reliability.narrow_valley.flood_affected_fraction_in_narrow_valley",
  );
  const opticalStatus = get(report, "optical.status");

  if (extentHa === undefined) {
    return {
      key: "reached",
      question: "Where did the flood reach?",
      pieces: [],
      layers: ["flood", "low_confidence"],
      unavailable: "This run has no flood extent yet.",
      sources: [],
    };
  }

  const pieces: Piece[] = [
    { text: "The flood reached" },
    { value: measure(extentHa), unit: "ha", source: `${REPORT}: flood_extent.flood_extent_ha` },
    { text: "of the valley floor —" },
    { value: measure(high), unit: "ha", source: `${REPORT}: areas_ha` },
    { text: "mapped with high confidence and" },
    { value: measure(medium), unit: "ha", source: `${REPORT}: areas_ha` },
    { text: "with medium." },
  ];

  const caveats: string[] = [];
  if (Number.isFinite(narrowFraction)) {
    caveats.push(
      `${percent(narrowFraction)} of it sits in a valley narrower than the radar can resolve.`,
    );
  }
  if (opticalStatus && opticalStatus !== "used") {
    const message = get(report, "optical.message");
    caveats.push(message ? `Radar only: ${message}.` : "Radar only for this run.");
  }

  return {
    key: "reached",
    question: "Where did the flood reach?",
    pieces,
    layers: ["flood", "low_confidence"],
    caveat: caveats.join(" ") || undefined,
    sources: [REPORT],
  };
}

function damage(report: RunReport | null): Sentence {
  const available = get(report, "damage.damage_available");
  const status = get(report, "damage.status");

  if (!available) {
    return {
      key: "broke",
      question: "What did it break?",
      pieces: [],
      layers: ["damaged_buildings", "cut_roads", "bridges_out"],
      unavailable:
        status && status !== "used"
          ? `Damage was not mapped for this run: ${String(status).replace(/_/g, " ")}.`
          : "Damage was not mapped for this run.",
      sources: [],
    };
  }

  const buildings = get(report, "damage.buildings.damaged");
  const roadsKm = get(report, "damage.roads.length_cut_km");
  const bridges = get(report, "damage.bridges.likely_out");
  const snapshot = get(report, "damage.osm.snapshot");

  return {
    key: "broke",
    question: "What did it break?",
    pieces: [
      { text: "It put" },
      {
        value: num(buildings),
        unit: buildings === 1 ? "building" : "buildings",
        source: `${REPORT}: damage.buildings.damaged`,
      },
      { text: "inside the flood, cut" },
      { value: measure(roadsKm), unit: "km of road", source: `${REPORT}: damage.roads.length_cut_km` },
      { text: "and left" },
      {
        value: num(bridges),
        unit: bridges === 1 ? "bridge" : "bridges",
        source: `${REPORT}: damage.bridges.likely_out`,
      },
      { text: "likely out." },
    ],
    layers: ["damaged_buildings", "cut_roads", "bridges_out"],
    caveat: `Inferred by crossing this run's flood extent with the OpenStreetMap snapshot of ${snapshot ?? "before the event"}. Bridges are likely out, unverified.`,
    sources: [REPORT],
  };
}

/** The single highest cut-off probability in the run, read verbatim off the
 *  settlement that carries it. A lookup, not a new statistic. */
function worstSettlement(
  settlements: LoadedLayer | undefined,
): { name: string; probability: number } | null {
  const features = settlements?.data?.features;
  if (!features || features.length === 0) return null;
  let best: { name: string; probability: number } | null = null;
  for (const feature of features) {
    const probability = feature.properties?.cut_off_probability;
    if (!Number.isFinite(probability)) continue;
    if (!best || probability > best.probability) {
      best = {
        name: feature.properties?.name || feature.properties?.kind || "an unnamed settlement",
        probability,
      };
    }
  }
  return best;
}

function cutoff(report: RunReport | null, settlements: LoadedLayer | undefined): Sentence {
  const available = get(report, "cutoff.cutoff_available");
  const status = get(report, "cutoff.status");

  if (!available) {
    return {
      key: "stranded",
      question: "Who is cut off?",
      pieces: [],
      layers: ["settlements", "routes"],
      unavailable:
        status && status !== "used"
          ? `Cut-off analysis was not run for this area: ${String(status).replace(/_/g, " ")}.`
          : "Cut-off analysis was not run for this area.",
      sources: [],
    };
  }

  const cutOff = get(report, "cutoff.cut_off_by_the_gated_definition");
  const total = get(report, "cutoff.settlements.total");
  const worst = worstSettlement(settlements);

  const pieces: Piece[] = [
    { value: num(cutOff), unit: cutOff === 1 ? "settlement" : "settlements" },
    { text: "of" },
    { value: num(total), unit: "", source: `${REPORT}: cutoff.settlements.total` },
    { text: "lost every road route to a health facility." },
  ];

  if (worst) {
    pieces.push(
      { text: "The highest cut-off probability is" },
      {
        value: percent(worst.probability, 0),
        unit: `at ${worst.name}`,
        source: "settlements.geojson: cut_off_probability",
      },
      { text: "." },
    );
  }

  const what = get(report, "cutoff.probability.what_it_is");

  return {
    key: "stranded",
    question: "Who is cut off?",
    pieces,
    layers: ["settlements", "routes"],
    caveat: what
      ? `Probability is a ${String(what).split(".")[0].replace(/^share/, "share")}.`
      : undefined,
    sources: [REPORT, "settlements.geojson"],
  };
}

export function buildSentences(
  report: RunReport | null,
  layers: Partial<Record<LayerName, LoadedLayer>>,
): Sentence[] {
  return [flood(report), damage(report), cutoff(report, layers.settlements)];
}

/** The stages the mission strip shows, and which report section proves each
 *  one actually happened. */
export const PIPELINE_STAGES: { key: string; name: string; proof: (r: RunReport | null) => boolean }[] =
  [
    { key: "preflight", name: "Satellite passes", proof: (r) => !!get(r, "track.post.day") },
    { key: "fetch", name: "Radar scenes", proof: (r) => !!get(r, "detection_stats.mode") },
    { key: "prepare", name: "Terrain", proof: (r) => Number.isFinite(get(r, "terrain.stream_pixels")) },
    { key: "classify", name: "Flood extent", proof: (r) => Number.isFinite(get(r, "flood_extent.flood_extent_ha")) },
    { key: "damage", name: "Damage", proof: (r) => !!get(r, "damage.damage_available") },
    { key: "cutoff", name: "Cut-off settlements", proof: (r) => !!get(r, "cutoff.cutoff_available") },
  ];
