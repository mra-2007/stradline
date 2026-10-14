/* "Ask about this area": the grounded copilot, in English or Nepali.

   Three ideas are adapted from reference/satquery-ai-main (AnswerPanel,
   TraceDrawer, VerificationPanel) and none of its styling:
     - the answer and its numbers are one object, so a number can be clicked
       and shown on the map;
     - the trace is collapsed by default and expands step by step;
     - the verifier's own result is stated, not implied.

   The copilot never writes a number: the model writes a template, code fills
   it from the evidence, and a verifier checks every digit in the finished text
   against that evidence. The panel says so where it can be read. */

import { useEffect, useRef, useState } from "react";

import { ApiError, api, type Answer, type AnswerNumber, type TraceStep } from "../lib/api";

interface Props {
  open: boolean;
  runId: string;
  onClose(): void;
  onFocusNumber(featureIds: string[]): boolean;
  onClearFocus(): void;
}

type Lang = "en" | "ne";

interface Turn {
  question: string;
  lang: Lang;
  answer: Answer | null;
  offline: boolean;
  problem: { message: string; hint: string } | null;
}

const STARTERS: Record<Lang, string[]> = {
  en: [
    "How much area is flooded?",
    "How many buildings are damaged?",
    "Which settlements are cut off?",
  ],
  ne: [
    "कति क्षेत्र डुबेको छ?",
    "कति घर क्षतिग्रस्त छन्?",
    "कुन बस्तीहरू सम्पर्कविहीन छन्?",
  ],
};

/** Split the answer text so every number the copilot reported becomes its own
 *  clickable piece. Longest formatted strings first, so "1,234" is not eaten
 *  by "1". */
function segment(text: string, numbers: AnswerNumber[]): (string | AnswerNumber)[] {
  const ordered = [...numbers]
    .filter((n) => n.formatted)
    .sort((a, b) => b.formatted.length - a.formatted.length);

  let parts: (string | AnswerNumber)[] = [text];
  for (const number of ordered) {
    const next: (string | AnswerNumber)[] = [];
    let placed = false;
    for (const part of parts) {
      if (typeof part !== "string" || placed) {
        next.push(part);
        continue;
      }
      const at = part.indexOf(number.formatted);
      if (at === -1) {
        next.push(part);
        continue;
      }
      if (at > 0) next.push(part.slice(0, at));
      next.push(number);
      const rest = part.slice(at + number.formatted.length);
      if (rest) next.push(rest);
      placed = true;
    }
    parts = next;
  }
  return parts;
}

function stepTitle(step: TraceStep): string {
  switch (step.step) {
    case "question":
      return "The question, as asked";
    case "evidence":
      return "Evidence loaded from the run";
    case "plan":
      return "What was planned";
    case "write":
      return "Who wrote the words";
    case "verify":
      return "Every number checked against the evidence";
    case "timing":
      return "Time taken";
    case "refuse":
      return "Why this was refused";
    default:
      return `Tool: ${String(step.tool ?? step.step)}`;
  }
}

function stepDetail(step: TraceStep): string {
  const d = step as Record<string, any>;
  switch (step.step) {
    case "question":
      return `${d.question} (${d.lang === "ne" ? "Nepali" : "English"})`;
    case "evidence":
      return `${Number(d.facts ?? 0).toLocaleString("en-GB")} facts from run ${d.run_id}, parameters hash ${d.params_hash}.${
        Array.isArray(d.missing) && d.missing.length ? ` Missing: ${d.missing.join(", ")}.` : ""
      }`;
    case "plan":
      return `${
        d.source === "keyword"
          ? "The language model was unavailable, so the offline keyword planner chose the tools."
          : "The language model chose which tools to run. It never writes a number."
      } Tools: ${(d.plan ?? []).map((s: any) => s.tool).join(", ") || "none"}.`;
    case "write":
      return d.source === "offline"
        ? "The wording came from the built-in templates, not from the model."
        : "The model wrote the wording around placeholders; code filled in the numbers.";
    case "verify":
      return d.ok
        ? `Passed. ${(d.matched ?? []).length} number${(d.matched ?? []).length === 1 ? "" : "s"} in the text, every one found in the evidence.`
        : `Failed: ${(d.findings ?? []).join("; ")}. The answer was rejected.`;
    case "timing":
      return `${Number(d.seconds ?? 0).toFixed(1)} seconds.`;
    default:
      return [
        d.template ? `Template: ${d.template}.` : "",
        Array.isArray(d.fact_ids) && d.fact_ids.length
          ? `Facts used: ${d.fact_ids.slice(0, 6).join(", ")}${d.fact_ids.length > 6 ? ` and ${d.fact_ids.length - 6} more` : ""}.`
          : "",
        Array.isArray(d.feature_ids) && d.feature_ids.length
          ? `${d.feature_ids.length} features named.`
          : "",
      ]
        .filter(Boolean)
        .join(" ");
  }
}

function Trace({ trace }: { trace: TraceStep[] }) {
  const [open, setOpen] = useState(false);
  const verify = trace.find((step) => step.step === "verify") as Record<string, any> | undefined;

  return (
    <div className="trace">
      <button
        type="button"
        className="btn btn--quiet trace__toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? "Hide how this was answered" : "How this was answered"}
      </button>
      {verify && (
        <p className={`trace__verdict caption${verify.ok ? "" : " trace__verdict--bad"}`}>
          {verify.ok
            ? `Verified: ${(verify.matched ?? []).length} numbers, all traced to the run's own files.`
            : "Not verified — the answer was refused."}
        </p>
      )}
      {open && (
        <ol className="trace__steps">
          {trace.map((step, index) => (
            <li key={index}>
              <p className="trace__step-title">{stepTitle(step)}</p>
              <p className="caption">{stepDetail(step)}</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export function AskPanel(props: Props) {
  const { open, runId } = props;
  const [lang, setLang] = useState<Lang>("en");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [focused, setFocused] = useState<string | null>(null);
  const log = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setTurns([]);
    setFocused(null);
  }, [runId]);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" });
  }, [turns, busy]);

  const ask = async (text: string) => {
    const asked = text.trim();
    if (!asked || busy) return;
    setQuestion("");
    setBusy(true);
    setTurns((previous) => [
      ...previous,
      { question: asked, lang, answer: null, offline: false, problem: null },
    ]);
    try {
      const answer = await api.ask(runId, asked, lang);
      setTurns((previous) =>
        previous.map((turn, index) =>
          index === previous.length - 1 ? { ...turn, answer } : turn,
        ),
      );
    } catch (err) {
      // The model-backed call timed out or the API errored. Fall back to the
      // offline templates, which never touch the network, rather than leaving
      // the question unanswered.
      try {
        const answer = await api.askOffline(runId, asked, lang);
        setTurns((previous) =>
          previous.map((turn, index) =>
            index === previous.length - 1 ? { ...turn, answer, offline: true } : turn,
          ),
        );
      } catch (offlineErr) {
        const problem =
          offlineErr instanceof ApiError
            ? { message: offlineErr.message, hint: offlineErr.hint }
            : { message: "The copilot could not answer.", hint: "" };
        setTurns((previous) =>
          previous.map((turn, index) =>
            index === previous.length - 1 ? { ...turn, problem } : turn,
          ),
        );
      }
    } finally {
      setBusy(false);
    }
  };

  const clickNumber = (number: AnswerNumber, key: string) => {
    const ids = number.feature_ids ?? [];
    if (ids.length === 0) return;
    if (focused === key) {
      props.onClearFocus();
      setFocused(null);
      return;
    }
    if (props.onFocusNumber(ids)) setFocused(key);
  };

  return (
    <aside
      className={`ask panel${open ? " ask--open" : ""}`}
      aria-label="Ask about this area"
      aria-hidden={!open}
    >
      <div className="panel__head">
        <h1>Ask about this area</h1>
      </div>

      <div className="ask__lang" role="group" aria-label="Answer language">
        <button
          type="button"
          className={`btn${lang === "en" ? " btn--primary" : ""}`}
          onClick={() => setLang("en")}
          aria-pressed={lang === "en"}
        >
          English
        </button>
        <button
          type="button"
          className={`btn${lang === "ne" ? " btn--primary" : ""}`}
          onClick={() => setLang("ne")}
          aria-pressed={lang === "ne"}
          lang="ne"
        >
          नेपाली
        </button>
      </div>

      <div className="ask__log" ref={log}>
        {turns.length === 0 && (
          <div className="ask__empty">
            <p className="caption">
              Answers are built from this run's own files. The model picks the wording; the numbers
              are filled in by code and checked one by one before you see them.
            </p>
            <ul className="ask__starters">
              {STARTERS[lang].map((starter) => (
                <li key={starter}>
                  <button
                    type="button"
                    className="btn btn--quiet"
                    lang={lang}
                    onClick={() => void ask(starter)}
                  >
                    {starter}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {turns.map((turn, turnIndex) => (
          <div className="turn" key={turnIndex}>
            <p className="turn__question" lang={turn.lang}>
              {turn.question}
            </p>

            {turn.problem && (
              <div className="notice notice--bad">
                <p>{turn.problem.message}</p>
                {turn.problem.hint && <p className="caption">{turn.problem.hint}</p>}
              </div>
            )}

            {!turn.answer && !turn.problem && (
              <p className="turn__waiting caption">
                Reading the run and checking every number…
              </p>
            )}

            {turn.answer && (
              <>
                {turn.offline && (
                  <p className="tag tag--offline" title="The model-backed call timed out or failed, so this answer came from the offline templates instead.">
                    offline mode
                  </p>
                )}
                <p
                  className={`turn__answer${turn.answer.refused ? " turn__answer--refused" : ""}`}
                  lang={turn.answer.lang}
                >
                  {segment(turn.answer.text, turn.answer.numbers).map((part, partIndex) =>
                    typeof part === "string" ? (
                      <span key={partIndex}>{part}</span>
                    ) : (
                      <button
                        type="button"
                        key={partIndex}
                        className={`figure${
                          focused === `${turnIndex}:${partIndex}` ? " figure--on" : ""
                        }${(part.feature_ids ?? []).length === 0 ? " figure--flat" : ""}`}
                        title={
                          (part.feature_ids ?? []).length > 0
                            ? `${part.fact_id} from ${part.source_file ?? "the run"} — show its ${(part.feature_ids ?? []).length} features on the map`
                            : `${part.fact_id} from ${part.source_file ?? "the run"}`
                        }
                        onClick={() => clickNumber(part, `${turnIndex}:${partIndex}`)}
                      >
                        {part.formatted}
                      </button>
                    ),
                  )}
                </p>
                <Trace trace={turn.answer.trace} />
              </>
            )}
          </div>
        ))}
      </div>

      <form
        className="ask__form"
        onSubmit={(event) => {
          event.preventDefault();
          void ask(question);
        }}
      >
        <input
          ref={input}
          type="text"
          lang={lang}
          value={question}
          placeholder={lang === "en" ? "Ask a question" : "प्रश्न सोध्नुहोस्"}
          onChange={(event) => setQuestion(event.target.value)}
          aria-label={lang === "en" ? "Your question" : "तपाईंको प्रश्न"}
        />
        <button type="submit" className="btn btn--primary" disabled={busy || !question.trim()}>
          {busy ? "Asking" : "Ask"}
        </button>
      </form>

      <div className="ask__sitrep">
        <span className="label">Download situation report</span>
        <div className="ask__sitrep-buttons">
          <a className="btn" href={api.sitrepUrl(runId, "en")} target="_blank" rel="noreferrer">
            English
          </a>
          <a
            className="btn"
            href={api.sitrepUrl(runId, "ne")}
            target="_blank"
            rel="noreferrer"
            lang="ne"
          >
            नेपाली
          </a>
        </div>
      </div>
    </aside>
  );
}
