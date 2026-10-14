/* The mission strip: what area, what date, the two actions, and the pipeline
   as a real numbered sequence with live progress. */

import { useState } from "react";

import { PIPELINE_STAGES } from "../lib/answers";
import type { RunReport, RunSummary } from "../lib/api";
import { bboxLabel, bboxSpanKm, longDate, runLabel } from "../lib/format";
import type { Analysis } from "../lib/useAnalysis";

type StageState = "waiting" | "running" | "done" | "stopped";

interface Props {
  report: RunReport | null;
  runId: string;
  runs: RunSummary[];
  loading: boolean;
  problem: { message: string; hint: string } | null;
  analysis: Analysis;
  drawMode: boolean;
  onDraw(): void;
  onSwitchRun(runId: string): void;
  onRecentre(): void;
}

/** What the strip says about each stage: the report proves the finished ones,
 *  a live run's `stages` list proves the one in flight. */
function stageStates(report: RunReport | null, analysis: Analysis): StageState[] {
  const live = analysis.phase === "running" || analysis.phase === "starting";
  const stopped = analysis.phase === "stopped";
  const reached = analysis.status?.stages ?? [];

  return PIPELINE_STAGES.map((stage, index) => {
    if (live || stopped) {
      if (reached.includes(stage.key)) return stopped ? "stopped" : "done";
      const furthest = PIPELINE_STAGES.findIndex((s) => !reached.includes(s.key));
      if (index === furthest) return stopped ? "stopped" : "running";
      return "waiting";
    }
    return stage.proof(report) ? "done" : "waiting";
  });
}

const MARK: Record<StageState, string> = {
  waiting: "",
  running: "running",
  done: "done",
  stopped: "stopped",
};

export function MissionStrip(props: Props) {
  const { report, runId, runs, loading, problem, analysis, drawMode } = props;
  const bbox = (analysis.bbox ?? report?.bbox_wgs84) as number[] | undefined;
  const usingDrawn = analysis.bbox !== null;
  const date = usingDrawn ? analysis.date : report?.event_date;
  const states = stageStates(report, analysis);
  const track = report?.track;
  const chosen = analysis.preflight?.chosen;
  // On a phone the strip is one bar until it is asked to open.
  const [open, setOpen] = useState(false);

  return (
    <aside className="strip panel" aria-label="Mission">
      <div className="strip__head">
        <h1>{bboxLabel(bbox)}</h1>
        <p className="caption">
          {loading && !report
            ? "Loading the run…"
            : `${longDate(date)}, an area ${bboxSpanKm(bbox)}.`}
        </p>
      </div>

      {problem && (
        <div className="notice notice--bad">
          <p>{problem.message}</p>
          {problem.hint && <p className="caption">{problem.hint}</p>}
        </div>
      )}

      <button
        type="button"
        className="btn btn--quiet strip__more"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? "Hide the controls" : "Map another flood"}
      </button>

      <div className="strip__collapse" data-open={open ? "true" : "false"}>
      <div className="strip__actions">
        <button
          type="button"
          className={`btn btn--block${drawMode ? " btn--primary" : ""}`}
          onClick={props.onDraw}
          aria-pressed={drawMode}
        >
          {drawMode ? "Drag a box on the map" : usingDrawn ? "Draw another area" : "Draw a new area"}
        </button>

        <label className="field">
          <span className="label">Flood date</span>
          <input
            type="date"
            value={analysis.date}
            max="2030-12-31"
            onChange={(event) => analysis.setDate(event.target.value)}
          />
        </label>

        <button
          type="button"
          className="btn btn--block"
          disabled={!usingDrawn || !analysis.date || analysis.phase === "checking"}
          onClick={() => void analysis.checkPasses()}
        >
          {analysis.phase === "checking" ? "Checking passes…" : "Check satellite passes"}
        </button>

        <button
          type="button"
          className="btn btn--block btn--primary"
          disabled={
            !usingDrawn ||
            !analysis.date ||
            analysis.phase === "starting" ||
            analysis.phase === "running"
          }
          onClick={() => void analysis.mapThisFlood()}
        >
          {analysis.phase === "running" || analysis.phase === "starting"
            ? "Mapping the flood…"
            : "Map this flood"}
        </button>
      </div>

      {analysis.preflight && (
        <div className={`notice${chosen ? "" : " notice--warn"}`}>
          {chosen ? (
            <>
              <p>
                {String(chosen.track[2] ?? "").toLowerCase() === "ascending"
                  ? "Ascending"
                  : "Descending"}{" "}
                orbit <span className="num">{String(chosen.track[1])}</span>, imaged{" "}
                <span className="num">{chosen.latency_days}</span>{" "}
                {chosen.latency_days === 1 ? "day" : "days"} after the flood.
              </p>
              <p className="caption">
                After: {chosen.post}. Before: {chosen.pre.slice(0, 3).join(", ")}
                {chosen.pre.length > 3 ? ` and ${chosen.pre.length - 3} more` : ""}.
              </p>
            </>
          ) : (
            <>
              <p>No usable radar pass for this area and date.</p>
              <p className="caption">
                Strandline needs a before and after pass on the same orbit. Try a date within a few
                days of the flood, or a slightly larger area.
              </p>
            </>
          )}
        </div>
      )}

      {analysis.problem && (
        <div className="notice notice--bad">
          <p>{analysis.problem.message}</p>
          {analysis.problem.hint && <p className="caption">{analysis.problem.hint}</p>}
        </div>
      )}

      {analysis.status?.error && (
        <div className="notice notice--bad">
          <p>
            {analysis.status.state === "refused" ? "Strandline refused this run: " : "The run failed: "}
            {analysis.status.error}
          </p>
          <p className="caption">
            Nothing was mapped, on purpose. A refusal is better than a wrong map. Try another date
            or area.
          </p>
        </div>
      )}

      <ol className="stages">
        {PIPELINE_STAGES.map((stage, index) => (
          <li key={stage.key} className={`stage stage--${states[index]}`}>
            <span className="stage__index num">{index + 1}</span>
            <span className="stage__name">{stage.name}</span>
            <span className="stage__state caption">{MARK[states[index]]}</span>
          </li>
        ))}
      </ol>

      {track?.post?.day && (
        <p className="caption strip__provenance">
          Radar after the flood: {track.post.day}. Parameters hash {report?.parameters_hash}.
        </p>
      )}

      <div className="strip__foot">
        <label className="field">
          <span className="label">Run</span>
          <select value={runId} onChange={(event) => props.onSwitchRun(event.target.value)}>
            {runs.length === 0 && <option value={runId}>{runLabel(runId)}</option>}
            {runs.map((item) => (
              <option key={item.run_id} value={item.run_id}>
                {runLabel(item.run_id)} ({item.event_date})
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn btn--quiet" onClick={props.onRecentre}>
          Recentre the map
        </button>
      </div>
      </div>
    </aside>
  );
}
