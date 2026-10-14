/* A new analysis: an area, a date, a look at the satellite passes, then a run.
   The pipeline refuses sometimes; that refusal is the product working, so it
   is shown in full, with what to try next. */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, type Preflight, type RunStatus } from "./api";

export type Phase = "idle" | "checking" | "checked" | "starting" | "running" | "done" | "stopped";

export interface Analysis {
  bbox: [number, number, number, number] | null;
  date: string;
  phase: Phase;
  preflight: Preflight | null;
  status: RunStatus | null;
  runId: string | null;
  problem: { message: string; hint: string } | null;
  setBbox(bbox: [number, number, number, number] | null): void;
  setDate(date: string): void;
  checkPasses(): Promise<void>;
  mapThisFlood(): Promise<void>;
  reset(): void;
}

const POLL_MS = 5000;

export function useAnalysis(onFinished: (runId: string) => void): Analysis {
  const [bbox, setBbox] = useState<[number, number, number, number] | null>(null);
  const [date, setDate] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [problem, setProblem] = useState<{ message: string; hint: string } | null>(null);
  const timer = useRef<number | null>(null);

  const stopPolling = () => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  };

  useEffect(() => stopPolling, []);

  const fail = (err: unknown) => {
    setProblem(
      err instanceof ApiError
        ? { message: err.message, hint: err.hint }
        : { message: err instanceof Error ? err.message : "Something went wrong.", hint: "" },
    );
  };

  const checkPasses = useCallback(async () => {
    if (!bbox || !date) return;
    setPhase("checking");
    setProblem(null);
    setPreflight(null);
    try {
      setPreflight(await api.preflight(bbox, date));
      setPhase("checked");
    } catch (err) {
      fail(err);
      setPhase("idle");
    }
  }, [bbox, date]);

  const mapThisFlood = useCallback(async () => {
    if (!bbox || !date) return;
    setPhase("starting");
    setProblem(null);
    setStatus(null);
    try {
      const { run_id } = await api.createRun(bbox, date);
      setRunId(run_id);
      setPhase("running");

      const poll = async () => {
        try {
          const next = await api.status(run_id);
          setStatus(next);
          if (next.state === "done") {
            stopPolling();
            setPhase("done");
            onFinished(run_id);
          } else if (next.state === "refused" || next.state === "failed") {
            stopPolling();
            setPhase("stopped");
          }
        } catch (err) {
          stopPolling();
          fail(err);
          setPhase("stopped");
        }
      };

      await poll();
      if (timer.current === null) timer.current = window.setInterval(poll, POLL_MS);
    } catch (err) {
      fail(err);
      setPhase("idle");
    }
  }, [bbox, date, onFinished]);

  const reset = useCallback(() => {
    stopPolling();
    setBbox(null);
    setPhase("idle");
    setPreflight(null);
    setStatus(null);
    setRunId(null);
    setProblem(null);
  }, []);

  return {
    bbox,
    date,
    phase,
    preflight,
    status,
    runId,
    problem,
    setBbox,
    setDate,
    checkPasses,
    mapThisFlood,
    reset,
  };
}
