/**
 * CoachReview.tsx
 * ───────────────
 * The trading coach's verdict on a trade the user has NOT yet made.
 *
 * Placed inside the trade form on purpose: after the rationale is written but
 * before "Log trade" is clicked is the only moment coaching can still change
 * the decision. Afterwards it is just a scorekeeper.
 *
 * Two presentational rules, both about not overstating what the coach knows:
 *   - `history_sufficient: false` is shown prominently. A coach that sounds
 *     equally confident with 2 trades and 200 teaches the user to trust it
 *     when it shouldn't be trusted.
 *   - `past_occurrences` dates are rendered as evidence chips. The backend has
 *     already stripped any date it could not match to a real journal entry, so
 *     everything displayed here is a trade the user actually made.
 */

import type { CoachReport } from "../../types";

/** Alignment is a score where HIGH is good, so the tone scale is inverted. */
function alignmentTone(score: number): "positive" | "negative" | "neutral" {
  if (score >= 67) return "positive";
  if (score <= 33) return "negative";
  return "neutral";
}

function severityTone(severity: string): "negative" | "neutral" {
  return severity === "strong" ? "negative" : "neutral";
}

export default function CoachReview({
  report,
  onDismiss,
}: {
  report: CoachReport;
  onDismiss: () => void;
}) {
  const tone = alignmentTone(report.alignment_score);

  return (
    <section className="coach-review">
      <header className="coach-head">
        <div className="coach-title">
          <span className="coach-icon">🧠</span>
          Coach review
          {report.proposed_action && (
            <span className="coach-subject">{report.proposed_action}</span>
          )}
        </div>
        <button className="btn-close" onClick={onDismiss} title="Dismiss review">
          ✕
        </button>
      </header>

      <div className="coach-align">
        <div className="coach-align-label">Alignment with the data</div>
        <div className={`coach-align-score tone-${tone}`}>
          {report.alignment_score}
          <span className="coach-align-max">/100</span>
        </div>
        <div className="coach-align-bar">
          <div
            className={`coach-align-fill coach-align-fill-${tone}`}
            style={{ width: `${Math.min(100, Math.max(0, report.alignment_score))}%` }}
          />
        </div>
      </div>

      {/* Never let a confident tone paper over a thin journal. */}
      {!report.history_sufficient && (
        <div className="coach-caution">
          Not enough trade history yet to identify a behavioural pattern — this
          review is based on the current trade and the available reports only.
        </div>
      )}

      <div className="coach-block">
        <h4 className="coach-block-title">Your rationale vs. the data</h4>
        <p className="coach-text">{report.rationale_evaluation}</p>
      </div>

      {report.detected_biases.length > 0 && (
        <div className="coach-block">
          <h4 className="coach-block-title">Possible biases</h4>
          <ul className="coach-bias-list">
            {report.detected_biases.map((b, i) => (
              <li key={i} className="coach-bias">
                <div className="coach-bias-head">
                  <span className={`coach-bias-name tone-${severityTone(b.severity)}`}>
                    {b.bias}
                  </span>
                  <span className="coach-bias-severity">{b.severity}</span>
                </div>
                <p className="coach-text coach-bias-evidence">{b.evidence}</p>
                {b.past_occurrences.length > 0 && (
                  <div className="coach-occurrences">
                    <span className="coach-occurrences-label">
                      Same pattern on:
                    </span>
                    {b.past_occurrences.map((d) => (
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

      {report.historical_pattern && (
        <div className="coach-block">
          <h4 className="coach-block-title">What your history shows</h4>
          <p className="coach-text">{report.historical_pattern}</p>
        </div>
      )}

      <div className="coach-block">
        <h4 className="coach-block-title">Coaching</h4>
        <p className="coach-text coach-feedback">{report.coaching_feedback}</p>
      </div>

      {report.supporting_data_points.length > 0 && (
        <div className="coach-block">
          <h4 className="coach-block-title">Based on</h4>
          <div className="coach-chips">
            {report.supporting_data_points.map((d, i) => (
              <span key={i} className="coach-chip">
                {d}
              </span>
            ))}
          </div>
        </div>
      )}

      {report.data_limitations.length > 0 && (
        <details className="coach-limits">
          <summary>What the coach could not see ({report.data_limitations.length})</summary>
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
