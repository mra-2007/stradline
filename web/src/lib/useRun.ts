/* Loading a run: its listing, its report, its layers — and nothing more.
   Every failure becomes a message with a hint, because a refusal is better
   than a silent wrong map (Rule 5). */

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  api,
  loadLayers,
  type LayerName,
  type LoadedLayer,
  type RunReport,
  type RunSummary,
} from "./api";

export interface RunData {
  runId: string;
  report: RunReport | null;
  layers: Partial<Record<LayerName, LoadedLayer>>;
  bbox: [number, number, number, number] | null;
  loading: boolean;
  problem: { message: string; hint: string } | null;
}

const asProblem = (err: unknown) =>
  err instanceof ApiError
    ? { message: err.message, hint: err.hint }
    : { message: err instanceof Error ? err.message : "Something went wrong.", hint: "" };

export function useRun(runId: string): RunData & { reload: () => void } {
  const [report, setReport] = useState<RunReport | null>(null);
  const [layers, setLayers] = useState<Partial<Record<LayerName, LoadedLayer>>>({});
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState<{ message: string; hint: string } | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setProblem(null);
    setReport(null);
    setLayers({});

    (async () => {
      try {
        const [nextReport, nextLayers] = await Promise.all([
          api.report(runId),
          loadLayers(runId),
        ]);
        if (!live) return;
        setReport(nextReport);
        setLayers(nextLayers);
      } catch (err) {
        if (live) setProblem(asProblem(err));
      } finally {
        if (live) setLoading(false);
      }
    })();

    return () => {
      live = false;
    };
  }, [runId, nonce]);

  const bbox = Array.isArray(report?.bbox_wgs84) && report!.bbox_wgs84.length === 4
    ? (report!.bbox_wgs84 as [number, number, number, number])
    : null;

  return {
    runId,
    report,
    layers,
    bbox,
    loading,
    problem,
    reload: useCallback(() => setNonce((n) => n + 1), []),
  };
}

export function useRunList(): { runs: RunSummary[]; reload: () => void } {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let live = true;
    api
      .listRuns()
      .then((body) => {
        if (live) setRuns(body.runs);
      })
      .catch(() => {
        if (live) setRuns([]);
      });
    return () => {
      live = false;
    };
  }, [nonce]);

  return { runs, reload: useCallback(() => setNonce((n) => n + 1), []) };
}
