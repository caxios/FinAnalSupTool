/**
 * JournalReview.tsx
 * ─────────────────
 * The coach's verdict on the user's WHOLE record rather than one decision.
 *
 * This answers what no per-trade review can: which behaviours actually recur,
 * whether well-reasoned trades have in fact done better, and what earlier
 * coaching was given and then ignored.
 *
 * Two presentational commitments, both matching the backend's:
 *   - `strengths` is shown first and never hidden. A review that lists only
 *     faults gets read once and then avoided, which costs the user more than
 *     any single missed correction.
 *   - `priorities` is short by construction (the backend caps it at three). A
 *     list of twelve things to fix is a list of zero things that get fixed.
 */

import type { JournalReport } from "../../types";

/** Trend is about a behaviour, so "improving" is the good direction. */
function trendTone(trend: string): "positive" | "negative" | "neutral" {
  if (trend === "improving") return "positive";
  if (trend === "worsening") return "negative";
  return "neutral";
}

function trendLabel(trend: string): string {
  if (trend === "improving") return "↘ easing";
  if (trend === "worsening") return "↗ hardening";
  return "→ steady";
}

export default function JournalReview({
  report,
  onDismiss,
}: {
  report: JournalReport;
  onDismiss: () => void;
}) {
  return (
    <section className="coach-review journal-review">
      <header className="coach-head">
        <div className="coach-title">
          <span className="coach-icon">🧠</span>
          Your record, reviewed
        </div>
        <button className="btn-close" onClick={onDismiss} title="Dismiss review">
          ✕
        </button>
      </header>

      <div className="journal-review-scope">
        {report.scope_description}
        {report.period && (
          <span className="journal-review-period">{report.period}</span>
        )}
      </div>

      {/* A confident-sounding review of six trades is what teaches misplaced trust. */}
      {!report.history_sufficient && (
        <div className="coach-caution">
          Too few logged trades to establish behavioural tendencies yet. Nothing
          below should be read as a pattern — it describes what is in the record,
          not what you habitually do.
        </div>
      )}

      {report.strengths.length > 0 && (
        <div className="coach-block">
          <h4 className="coach-block-title">What you are doing well</h4>
          <ul className="journal-review-list journal-review-strengths">
            {report.strengths.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}

      {report.recurring_patterns.length > 0 && (
        <div className="coach-block">
          <h4 className="coach-block-title">What keeps recurring</h4>
          <ul className="coach-bias-list">
            {report.recurring_patterns.map((p, i) => (
              <li key={i} className="coach-bias">
                <div className="coach-bias-head">
                  <span className="coach-bias-name">{p.pattern}</span>
                  <span
                    className={`coach-bias-severity tone-${trendTone(p.trend)}`}
                  >
                    {trendLabel(p.trend)}
                  </span>
                </div>
                {p.evidence && (
                  <p className="coach-text coach-bias-evidence">{p.evidence}</p>
                )}
                {p.occurrences.length > 0 && (
                  <div className="coach-occurrences">
                    <span className="coach-occurrences-label">Seen on:</span>
                    {p.occurrences.map((d) => (
                      <span key={d} className="coach-date-chip">
                        {d}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="coach-block">
        <h4 className="coach-block-title">Does good reasoning pay off here?</h4>
        <p className="coach-text">{report.process_vs_outcome}</p>
      </div>

      {report.advice_followed && (
        <div className="coach-block">
          <h4 className="coach-block-title">Advice given, and what you did</h4>
          <p className="coach-text coach-feedback">{report.advice_followed}</p>
        </div>
      )}

      {report.priorities.length > 0 && (
        <div className="coach-block">
          <h4 className="coach-block-title">
            What to work on next ({report.priorities.length})
          </h4>
          <ol className="journal-review-list journal-review-priorities">
            {report.priorities.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ol>
        </div>
      )}

      {report.data_limitations.length > 0 && (
        <details className="coach-limits">
          <summary>
            What the coach could not see ({report.data_limitations.length})
          </summary>
          <ul>
            {report.data_limitations.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        </details>
      )}

      <div className="coach-footer">
        The coach advises; the decision stays yours.
      </div>
    </section>
  );
}
