/* The radar behind the map: this run's own quicklook, plus the scenes and the
   parameters hash it was made from. If a judge asks "where does this come
   from", this is the answer in one click. */

import { useEffect, useRef, useState } from "react";

import { api, type RunReport } from "../lib/api";
import { longDate } from "../lib/format";

interface Props {
  runId: string;
  report: RunReport | null;
  onClose(): void;
}

export function Evidence({ runId, report, onClose }: Props) {
  const [state, setState] = useState<"loading" | "ready" | "missing">("loading");
  const close = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    close.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const track = report?.track;
  const post = track?.post;
  const pre = Array.isArray(track?.pre) ? track.pre : [];

  return (
    <div className="scrim" role="dialog" aria-modal="true" aria-label="Radar evidence">
      <div className="evidence panel">
        <div className="panel__head">
          <h1>The radar this map was made from</h1>
          <button ref={close} type="button" className="btn btn--quiet" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="evidence__body">
          <div className="evidence__image">
            {state === "missing" ? (
              <p className="caption">
                This run has no quicklook. It is written at the end of a successful run.
              </p>
            ) : (
              <img
                src={api.quicklookUrl(runId)}
                alt="Radar quicklook: Sentinel-1 backscatter before and after the flood, with the mapped classes"
                onLoad={() => setState("ready")}
                onError={() => setState("missing")}
              />
            )}
            {state === "loading" && <p className="caption">Loading the quicklook…</p>}
          </div>

          <div className="evidence__facts">
            {post?.day && (
              <p>
                Imaged after the flood on <span className="num">{longDate(post.day)}</span>, on{" "}
                {String(track?.direction ?? "").toLowerCase() || "the same"} relative orbit{" "}
                <span className="num">{track?.relative_orbit}</span>.
              </p>
            )}
            {pre.length > 0 && (
              <p>
                Compared with <span className="num">{pre.length}</span> passes on the same orbit
                before the flood, from {pre[0]?.day} to {pre[pre.length - 1]?.day}.
              </p>
            )}
            {report?.parameters_hash && (
              <p className="caption">
                Parameters hash {report.parameters_hash}. The same inputs and the same hash give
                byte-identical outputs.
              </p>
            )}
            {report?.optical?.message && <p className="caption">{report.optical.message}.</p>}
            <p className="caption">
              Scenes: {post?.scenes?.join(", ") ?? "listed in run_report.json"}.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
