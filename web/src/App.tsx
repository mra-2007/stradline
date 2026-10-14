/* The dashboard: a full-bleed map with three things on top of it. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Answers } from "./panels/Answers";
import { AskPanel } from "./panels/AskPanel";
import { Evidence } from "./panels/Evidence";
import { Footer } from "./panels/Footer";
import { MissionStrip } from "./panels/MissionStrip";
import { SettlementCard } from "./panels/SettlementCard";
import { Intro } from "./intro/Intro";
import { MapCanvas, type GroupKey, type MapHandle } from "./map/MapCanvas";
import type { Viewpoint } from "./map/flyIn";
import { prefersReducedMotion } from "./map/flyIn";
import type { GeoJSONFeature } from "./lib/api";
import { buildSentences } from "./lib/answers";
import { useAnalysis } from "./lib/useAnalysis";
import { useRun, useRunList } from "./lib/useRun";

const FIRST_RUN = "case_smoke";

/** Properties on a damage or settlement feature that can carry a copilot
 *  feature id. The copilot emits OSM ids for infrastructure and names for
 *  settlements, so both are matched. */
const ID_KEYS = ["@osmId", "segment_id", "name"] as const;

export function App() {
  const { runs, reload: reloadRuns } = useRunList();
  const [runId, setRunId] = useState(FIRST_RUN);
  const run = useRun(runId);

  const [visibleGroups, setVisibleGroups] = useState<Record<GroupKey, boolean>>({
    reached: true,
    broke: true,
    stranded: true,
  });
  const [showLowConfidence, setShowLowConfidence] = useState(true);
  const [selected, setSelected] = useState<Record<string, any> | null>(null);
  const [askOpen, setAskOpen] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [drawMode, setDrawMode] = useState(false);
  const [flying, setFlying] = useState(!prefersReducedMotion());
  const [showIntro, setShowIntro] = useState(false);
  const [openFrom, setOpenFrom] = useState<Viewpoint | null>(null);
  const viewpoint = useRef<Viewpoint | null>(null);
  const mapRef = useRef<MapHandle | null>(null);

  // The intro is the first thing on screen — unless the browser asks for less
  // motion, or the machine is too small to spare a WebGL scene for it.
  useEffect(() => {
    const lowPower =
      prefersReducedMotion() ||
      (navigator.hardwareConcurrency ?? 8) <= 2 ||
      Math.min(window.innerWidth, window.innerHeight) < 380;
    setShowIntro(!lowPower && !new URLSearchParams(window.location.search).has("nointro"));
  }, []);

  const analysis = useAnalysis(
    useCallback(
      (newRunId: string) => {
        setRunId(newRunId);
        reloadRuns();
      },
      [reloadRuns],
    ),
  );

  const sentences = useMemo(() => buildSentences(run.report, run.layers), [run.report, run.layers]);

  // The date field starts on the date of the run you are looking at, so
  // "Check satellite passes" is one click away from a drawn box.
  useEffect(() => {
    if (!analysis.date && typeof run.report?.event_date === "string") {
      analysis.setDate(run.report.event_date);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.report?.event_date]);

  const everyFeature = useMemo(() => {
    const out: GeoJSONFeature[] = [];
    for (const layer of Object.values(run.layers)) {
      if (layer?.data?.features) out.push(...layer.data.features);
    }
    return out;
  }, [run.layers]);

  /** Features whose id the copilot named. */
  const findFeatures = useCallback(
    (ids: string[]): GeoJSONFeature[] => {
      if (ids.length === 0) return [];
      const wanted = new Set(ids);
      return everyFeature.filter((feature) =>
        ID_KEYS.some((key) => {
          const value = feature.properties?.[key];
          return typeof value === "string" && wanted.has(value);
        }),
      );
    },
    [everyFeature],
  );

  const focusFeatures = useCallback(
    (ids: string[]) => {
      const features = findFeatures(ids);
      if (features.length === 0) return false;
      mapRef.current?.showHighlight(features);
      mapRef.current?.flyToFeatures(features);
      return true;
    },
    [findFeatures],
  );

  /** The routes this settlement has, as the pipeline wrote them. */
  const routesFor = useCallback(
    (name: string | undefined): GeoJSONFeature[] => {
      if (!name) return [];
      const features = run.layers.routes?.data?.features ?? [];
      return features
        .filter((feature) => feature.properties?.settlement === name)
        .map((feature, index) => ({
          ...feature,
          properties: { ...feature.properties, phase: index === 0 ? "before" : "after" },
        }));
    },
    [run.layers.routes],
  );

  const selectSettlement = useCallback(
    (properties: Record<string, any> | null) => {
      setSelected(properties);
      mapRef.current?.clearHighlight();
      if (!properties) {
        mapRef.current?.clearRouteFocus();
        return;
      }
      mapRef.current?.drawRouteFocus(routesFor(properties.name));
    },
    [routesFor],
  );

  // Switching runs clears anything that belonged to the old one.
  useEffect(() => {
    setSelected(null);
    setDrawMode(false);
    mapRef.current?.clearHighlight();
    mapRef.current?.clearRouteFocus();
  }, [runId]);

  const toggleGroup = (key: GroupKey) =>
    setVisibleGroups((groups) => ({ ...groups, [key]: !groups[key] }));

  return (
    <div className="shell">
      <MapCanvas
        runId={runId}
        bbox={run.bbox}
        layers={run.layers}
        visibleGroups={visibleGroups}
        showLowConfidence={showLowConfidence}
        selectedSettlement={selected?.name ?? null}
        drawMode={drawMode}
        openFrom={openFrom}
        onReady={(handle) => {
          mapRef.current = handle;
        }}
        onSelectSettlement={selectSettlement}
        onBoxDrawn={(bbox) => {
          analysis.setBbox(bbox);
          setDrawMode(false);
        }}
        onViewpoint={(next) => {
          viewpoint.current = next;
        }}
        onFlyInEnd={() => setFlying(false)}
      />

      {flying && !showIntro && (
        <button
          type="button"
          className="skip-flight"
          onClick={() => {
            mapRef.current?.skipFlyIn();
            setFlying(false);
          }}
        >
          Skip the fly-in
        </button>
      )}

      <div className={`furniture${flying ? " furniture--waiting" : ""}`}>
        <MissionStrip
          report={run.report}
          runId={runId}
          runs={runs}
          loading={run.loading}
          problem={run.problem}
          analysis={analysis}
          drawMode={drawMode}
          onDraw={() => setDrawMode(true)}
          onSwitchRun={setRunId}
          onRecentre={() => mapRef.current?.resetView()}
        />

        <Answers
          sentences={sentences}
          layers={run.layers}
          visibleGroups={visibleGroups}
          showLowConfidence={showLowConfidence}
          onToggleGroup={toggleGroup}
          onToggleLowConfidence={() => setShowLowConfidence((on) => !on)}
          loading={run.loading}
        />

        <div className="map-actions">
          <button type="button" className="btn" onClick={() => setEvidenceOpen(true)}>
            Evidence
          </button>
          <button
            type="button"
            className={`btn${askOpen ? "" : " btn--primary"}`}
            onClick={() => setAskOpen((open) => !open)}
            aria-expanded={askOpen}
          >
            {askOpen ? "Close questions" : "Ask about this area"}
          </button>
        </div>

        <AskPanel
          open={askOpen}
          runId={runId}
          onClose={() => setAskOpen(false)}
          onFocusNumber={focusFeatures}
          onClearFocus={() => mapRef.current?.clearHighlight()}
        />

        {selected && (
          <SettlementCard
            properties={selected}
            report={run.report}
            routes={routesFor(selected.name)}
            onClose={() => selectSettlement(null)}
          />
        )}

        <Footer report={run.report} />
      </div>

      {evidenceOpen && (
        <Evidence runId={runId} report={run.report} onClose={() => setEvidenceOpen(false)} />
      )}

      {showIntro && (
        <Intro
          runId={runId}
          bbox={run.bbox}
          onOpen={(from) => {
            setOpenFrom(from);
            setShowIntro(false);
            setFlying(false);
          }}
        />
      )}
    </div>
  );
}
