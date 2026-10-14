/* One settlement, picked on the map.

   Everything here is a property the pipeline wrote onto that settlement, shown
   under the definition the pipeline used: the gated definition is the vehicle
   graph, the nearest health facility, and the scenario where bridges hold.
   The route lengths are the pipeline's own before and after; where there is no
   route after the flood, that is what it says. */

import { useEffect, useState } from "react";

import type { GeoJSONFeature, RunReport } from "../lib/api";
import { measure, num, percent } from "../lib/format";

interface Props {
  properties: Record<string, any>;
  report: RunReport | null;
  routes: GeoJSONFeature[];
  onClose(): void;
}

const STATUS_WORDS: Record<string, string> = {
  "cut off": "Cut off",
  detour: "On a detour",
  reachable: "Reachable",
  "no route before the flood": "No mapped route, even before the flood",
};

function statusClass(status: string | undefined): string {
  if (status === "cut off") return "status--cut";
  if (status === "detour") return "status--detour";
  if (status === "reachable") return "status--ok";
  return "status--none";
}

function metres(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "no route";
  return value >= 1000 ? `${measure(value / 1000)} km` : `${num(value)} m`;
}

function Row({
  label,
  before,
  after,
  status,
}: {
  label: string;
  before: unknown;
  after: unknown;
  status: string | undefined;
}) {
  const lost = typeof after !== "number" || !Number.isFinite(after);
  return (
    <div className="route-row">
      <span className="label">{label}</span>
      <span className="route-row__values">
        <span className="readout">{metres(before)}</span>
        <span className="route-row__arrow caption">before</span>
        <span className={`readout${lost ? " readout--lost" : ""}`}>{metres(after)}</span>
        <span className="route-row__arrow caption">after</span>
      </span>
      <span className={`status ${statusClass(status)}`}>
        {STATUS_WORDS[status ?? ""] ?? status ?? "not computed"}
      </span>
    </div>
  );
}

export function SettlementCard({ properties, report, routes, onClose }: Props) {
  const [enter, setEnter] = useState(false);
  useEffect(() => {
    const frame = requestAnimationFrame(() => setEnter(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  const name = properties.name || "Unnamed settlement";
  const gated = properties.vehicle_health_bridges_intact_status as string | undefined;
  const probability = properties.cut_off_probability;
  const runs = report?.cutoff?.probability?.runs;
  const note = routes[0]?.properties?.note as string | undefined;

  return (
    <section className={`settlement panel${enter ? " settlement--in" : ""}`} aria-label={`Settlement ${name}`}>
      <div className="panel__head">
        <h1 lang={/[ऀ-ॿ]/.test(String(name)) ? "ne" : "en"}>{name}</h1>
        <button type="button" className="btn btn--quiet" onClick={onClose}>
          Close
        </button>
      </div>

      <div className="settlement__body">
        <p className="settlement__lead">
          <span className={`status status--lead ${statusClass(gated)}`}>
            {STATUS_WORDS[gated ?? ""] ?? gated ?? "Not computed"}
          </span>
          <span className="caption">
            {properties.kind === "building_cluster"
              ? "A cluster of buildings, not a named place."
              : `Mapped in OpenStreetMap as a ${String(properties.kind ?? "place")}.`}
            {Number.isFinite(properties.n_buildings)
              ? ` ${num(properties.n_buildings)} buildings.`
              : ""}
          </span>
        </p>

        {Number.isFinite(probability) && (
          <div className="settlement__probability">
            <div className="prob">
              <div className="prob__bar">
                <span style={{ width: `${Math.max(2, Math.min(100, probability * 100))}%` }} />
              </div>
              <span className="readout prob__value">{percent(probability, 0)}</span>
            </div>
            <p className="caption">
              {Number.isFinite(runs)
                ? `Cut off in ${percent(probability, 0)} of ${num(runs)} perturbed runs — `
                : ""}
              {properties.probability_is ?? "a sensitivity measure, not a calibrated chance"}.
            </p>
          </div>
        )}

        <Row
          label="By vehicle, to a health facility"
          before={properties.vehicle_health_before_m}
          after={properties.vehicle_health_bridges_intact_after_m}
          status={properties.vehicle_health_bridges_intact_status}
        />
        <Row
          label="On foot, to a health facility"
          before={properties.foot_health_before_m}
          after={properties.foot_health_bridges_intact_after_m}
          status={properties.foot_health_bridges_intact_status}
        />
        <Row
          label="By vehicle, to the nearest town"
          before={properties.vehicle_town_before_m}
          after={properties.vehicle_town_bridges_intact_after_m}
          status={properties.vehicle_town_bridges_intact_status}
        />

        {gated === "cut off" && (
          <p className="settlement__verdict">
            No route to the nearest health facility after the flood.
          </p>
        )}

        <p className="caption settlement__caveat">
          {routes.length > 0
            ? `Drawn on the map: ${note ?? "the link from this settlement to the road it routes from"}. The pipeline does not store the full path, so no path is drawn.`
            : "This settlement has no route link in this run, so nothing is drawn on the map."}
        </p>
      </div>
    </section>
  );
}
