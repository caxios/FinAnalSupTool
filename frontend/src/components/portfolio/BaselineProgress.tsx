/**
 * BaselineProgress.tsx
 * ────────────────────
 * What the app is doing in the background for a newly added holding.
 *
 * Adding a position kicks off two long jobs: ingesting ~2 years of SEC filings,
 * then one full Deep Analysis per completed quarter over that same span. Both
 * take minutes, and until this existed the user saw a single line at submit time
 * and then nothing — with no way to tell "still working" from "silently failed".
 *
 * The analyses themselves are read in the **Deep Analysis** view, which already
 * lists every stored run per company. This component's job is only to say what
 * exists and get the user there.
 */

import { NavLink } from "react-router-dom";
import type { BaselineStatus } from "../../types";

const RUNNING = new Set(["queued", "running", "pending"]);

/** Whether anything is still in flight — the caller polls while this is true. */
export function baselineInFlight(
  statuses: Record<string, BaselineStatus> | undefined
): boolean {
  return Object.values(statuses ?? {}).some(
    (s) => RUNNING.has(s.state) || RUNNING.has(s.analysis?.state ?? "")
  );
}

function stateTone(state: string): "positive" | "negative" | "neutral" {
  if (state === "complete") return "positive";
  if (state === "failed") return "negative";
  return "neutral";
}

export default function BaselineProgress({
  statuses,
  onSelectTicker,
}: {
  statuses: Record<string, BaselineStatus>;
  onSelectTicker: (ticker: string) => void;
}) {
  const entries = Object.entries(statuses ?? {}).filter(
    ([, s]) => s.state !== "none"
  );
  if (entries.length === 0) return null;

  return (
    <div className="baseline-panel">
      {entries.map(([ticker, s]) => {
        const a = s.analysis;
        const pct =
          a && a.total > 0 ? Math.round((a.completed / a.total) * 100) : 0;
        const done = a?.state === "complete" || a?.state === "partial";

        return (
          <div key={ticker} className="baseline-row">
            <div className="baseline-head">
              <span className="baseline-ticker">{ticker}</span>
              <span className={`baseline-state tone-${stateTone(s.state)}`}>
                {s.state === "unsupported" ? "no SEC data" : s.state}
              </span>
              {typeof s.ingested === "number" && s.ingested > 0 && (
                <span className="baseline-count">{s.ingested} filings</span>
              )}
            </div>

            <p className="baseline-message">{s.message}</p>

            {a && a.state !== "skipped" && (
              <>
                <div className="baseline-analysis-head">
                  <span className="baseline-analysis-label">
                    Quarterly Deep Analysis
                  </span>
                  <span className="baseline-analysis-count">
                    {a.completed} / {a.total}
                  </span>
                </div>
                <div className="baseline-bar">
                  <div
                    className={`baseline-bar-fill ${
                      a.state === "failed" ? "baseline-bar-failed" : ""
                    }`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <p className="baseline-message">{a.message}</p>

                {/* Each run analyzes data up to one quarter's close, which is
                    what lets the coach cite it when reviewing a trade made
                    after that quarter without seeing anything that came later. */}
                {done && (
                  <NavLink
                    to="/analysis"
                    className="baseline-link"
                    onClick={() => onSelectTicker(ticker)}
                  >
                    Read the {ticker} analyses →
                  </NavLink>
                )}

                {a.failures && a.failures.length > 0 && (
                  <details className="baseline-failures">
                    <summary>
                      {a.failures.length} quarter
                      {a.failures.length === 1 ? "" : "s"} failed
                    </summary>
                    <ul>
                      {a.failures.map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
