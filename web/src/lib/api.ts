/* The only place that talks to the Strandline API.
   Contract: api/API_CONTRACT.md. Every path is relative, because the built
   bundle is served by the API itself. */

export type RunState = "queued" | "running" | "done" | "refused" | "failed";

export interface RunSummary {
  run_id: string;
  bbox: [number, number, number, number];
  event_date: string;
  state: RunState;
  created_at: string;
}

export interface RunStatus {
  state: RunState;
  stages: string[];
  error: string | null;
}

export interface PreflightChoice {
  track: [string, number, string] | unknown[];
  post: string;
  latency_days: number;
  pre: string[];
  notes: string[];
}

export interface Preflight {
  chosen: PreflightChoice | null;
  message: string;
}

export interface AnswerNumber {
  value: number;
  unit: string;
  fact_id: string;
  formatted: string;
  feature_ids?: string[];
  source_file?: string;
  derivation?: string;
}

export interface TraceStep {
  step: string;
  [key: string]: unknown;
}

export interface Answer {
  text: string;
  numbers: AnswerNumber[];
  trace: TraceStep[];
  refused: boolean;
  reason: string | null;
  lang: "en" | "ne";
}

/** Whatever the pipeline wrote. Read defensively: a field may be absent. */
export type RunReport = Record<string, any>;

export type FeatureCollection = {
  type: "FeatureCollection";
  features: GeoJSONFeature[];
};

export type GeoJSONFeature = {
  type: "Feature";
  id?: string | number;
  geometry: any;
  properties: Record<string, any>;
};

/** An API error carrying the server's own `error` and `hint`. */
export class ApiError extends Error {
  status: number;
  hint: string;

  constructor(status: number, message: string, hint: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.hint = hint;
  }
}

const BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

export function apiUrl(path: string): string {
  return `${BASE}${path}`;
}

/** How long the dashboard waits for an answer before it says so.
 *  One answer costs the copilot two completions, each capped at
 *  `CopilotParams.timeout_s` (8 s), and the offline fallback is immediate, so
 *  the server's own worst case is well inside this. If this fires, something
 *  other than the language model is stuck. */
export const ASK_TIMEOUT_MS = 20_000;

async function request<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  let response: Response;
  const controller = timeoutMs ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : undefined;
  try {
    response = await fetch(apiUrl(path), { ...init, signal: controller?.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(
        0,
        `No answer in ${Math.round((timeoutMs ?? 0) / 1000)} seconds, so the question was dropped.`,
        "The copilot answers from its own templates within seconds when the model is " +
          "unavailable, so this means the API itself is stuck. Ask again, or restart it.",
      );
    }
    throw new ApiError(
      0,
      "The Strandline API did not answer.",
      "Start it with: venv\\Scripts\\python.exe -m uvicorn api.main:app --port 8000",
    );
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    let hint = "";
    try {
      const body = await response.json();
      message = body.error ?? message;
      hint = body.hint ?? "";
    } catch {
      /* a non-JSON error body; the status line is all we have */
    }
    throw new ApiError(response.status, message, hint);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string }>("/health"),

  listRuns: () => request<{ runs: RunSummary[] }>("/runs"),

  status: (runId: string) => request<RunStatus>(`/runs/${runId}/status`),

  report: (runId: string) => request<RunReport>(`/runs/${runId}/report`),

  layer: (runId: string, name: LayerName) =>
    request<FeatureCollection>(`/runs/${runId}/layers/${name}`),

  preflight: (bbox: number[], date: string) =>
    request<Preflight>(`/preflight?bbox=${bbox.join(",")}&date=${date}`),

  createRun: (bbox: number[], eventDate: string) =>
    request<{ run_id: string }>("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bbox, event_date: eventDate }),
    }),

  ask: (runId: string, question: string, lang: "en" | "ne") =>
    request<Answer>(
      `/runs/${runId}/ask`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, lang }),
      },
      ASK_TIMEOUT_MS,
    ),

  /** Skips the model-backed call entirely and answers from the offline
   *  templates, which never touch the network and so never time out. Used
   *  as the fallback when `ask` itself times out or fails. */
  askOffline: (runId: string, question: string, lang: "en" | "ne") =>
    request<Answer>(
      `/runs/${runId}/ask`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, lang, offline: true }),
      },
      ASK_TIMEOUT_MS,
    ),

  sitrepUrl: (runId: string, lang: "en" | "ne") =>
    apiUrl(`/runs/${runId}/sitrep?lang=${lang}`),

  quicklookUrl: (runId: string) => apiUrl(`/runs/${runId}/quicklook`),

  terrainTiles: (runId: string) => apiUrl(`/runs/${runId}/terrain/{z}/{x}/{y}.png`),
};

export const LAYER_NAMES = [
  "flood",
  "low_confidence",
  "damaged_buildings",
  "cut_roads",
  "bridges_out",
  "settlements",
  "routes",
] as const;

export type LayerName = (typeof LAYER_NAMES)[number];

export interface LoadedLayer {
  name: LayerName;
  data: FeatureCollection | null;
  /** Why it is not here, in the API's own words. */
  missing: string | null;
}

/** Fetch every vector layer at once. A 404 is an answer, not a failure: the
 *  run simply did not produce that layer, and the UI says so. */
export async function loadLayers(runId: string): Promise<Record<LayerName, LoadedLayer>> {
  const entries = await Promise.all(
    LAYER_NAMES.map(async (name): Promise<[LayerName, LoadedLayer]> => {
      try {
        const data = await api.layer(runId, name);
        return [name, { name, data, missing: null }];
      } catch (err) {
        const missing =
          err instanceof ApiError && err.status === 404
            ? "not produced by this run"
            : err instanceof Error
              ? err.message
              : "could not be loaded";
        return [name, { name, data: null, missing }];
      }
    }),
  );
  return Object.fromEntries(entries) as Record<LayerName, LoadedLayer>;
}
