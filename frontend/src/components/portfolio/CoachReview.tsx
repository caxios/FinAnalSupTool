/**
 * CoachReview.tsx
 * ───────────────
 * The trading coach's verdict on one decision, in either of two modes.
 *
 * **Pre-trade** — rendered inside the trade form. After the rationale is written
 * but before "Log trade" is clicked is the only moment coaching can still change
 * the decision.
 *
 * **Retrospective** — rendered inside a journal row, for a trade already logged.
 * Here the headline number is `process_quality`, which the backend produced
 * *before* the model was shown what happened next, and the outcome is reported
 * beside it rather than folded into it. That separation is the point: a decision
 * can be sound and still lose money, and a review that cannot say so teaches the
 * user to chase outcomes.
 *
 * Three presentational rules, all about not overstating what the coach knows:
 *   - `history_sufficient: false` is shown prominently. A coach that sounds
 *     equally confident with 2 trades and 200 teaches the user to trust it
 *     when it shouldn't be trusted.
 *   - `past_occurrences` dates are rendered as evidence chips. The backend has
 *     already stripped any date it could not match to a real journal entry, so
 *     everything displayed here is a trade the user actually made.
 *   - The four-quadrant verdict is coloured by the PROCESS, never by the money.
 *     "Bad process, good outcome" is the most dangerous result there is and must
 *     not be shown in the same green as a decision that was actually sound.
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

/**
 * The four quadrants, and how each should FEEL.
 *
 * Tone follows the process, never the outcome. "Bad process, good outcome" is
 * the most dangerous cell there is — a bad habit just got paid — so it must not
 * render as a success. Colouring by outcome here would teach exactly the
 * outcome-chasing the two-pass review exists to prevent.
 */
const QUADRANTS: Record<
  string,
  { label: string; tone: "positive" | "negative" | "neutral"; note: string }
> = {
  "good process, good outcome": {
    label: "Sound decision, and it worked",
    tone: "positive",
    note: "Repeat this.",
  },
  "good process, bad outcome": {
    label: "Sound decision, bad luck",
    tone: "positive",
    note: "The reasoning held up. Losing money on a good decision is not a reason to change the process.",
  },
  "bad process, good outcome": {
    label: "Got away with it",
    tone: "negative",
    note: "This made money despite the reasoning, not because of it — the most dangerous kind of result, because it rewards the habit.",
  },
  "bad process, bad outcome": {
    label: "The reasoning did not hold up",
    tone: "negative",
    note: "The process is what to fix here.",
  },
  "too early to tell": {
    label: "Too early to tell",
    tone: "neutral",
    note: "No outcome horizon has elapsed yet.",
  },
};

export default function CoachReview({
  report,
  onDismiss,
}: {
  report: CoachReport;
  /** Omitted when the review is embedded in a journal row rather than a form. */
  onDismiss?: () => void;
}) {
  const retro = report.review_type === "retrospective";

  // On a retrospective the headline number is the PROCESS score, which was
  // produced before the model was shown what happened next.
  const score = retro
    ? report.process_quality ?? report.alignment_score
    : report.alignment_score;
  const tone = alignmentTone(score);
  const quadrant = report.luck_vs_skill
    ? QUADRANTS[report.luck_vs_skill.trim().toLowerCase()]
    : undefined;

  return (
    <section className="coach-review">
      <header className="coach-head">
        <div className="coach-title">
          <span className="coach-icon">🧠</span>
          {retro ? "Looking back at this trade" : "Coach review"}
          {report.proposed_action && (
            <span className="coach-subject">{report.proposed_action}</span>
          )}
        </div>
        {onDismiss && (
          <button className="btn-close" onClick={onDismiss} title="Dismiss review">
            ✕
          </button>
        )}
      </header>

      {/* Prominent, empirical — "the last N times you did this, you lost
          money", never a generic lecture. This is the single most important
          thing in a pre-trade review when present, so it renders before even
          the alignment score. */}
      {report.toxic_pattern_matches.length > 0 && (
        <div className="coach-rule-banner coach-rule-banner-toxic">
          <div className="coach-rule-banner-title">
            ⚠ Matches your own Toxic Pattern
          </div>
          {report.toxic_pattern_matches.map((m) => (
            <div key={m.id} className="coach-rule-match">
              <div className="coach-rule-match-title">{m.title}</div>
              <div className="coach-rule-match-stats">
                {m.win_rate !== null && <span>{(m.win_rate * 100).toFixed(0)}% win rate</span>}
                {m.expectancy !== null && (
                  <span>expectancy {m.expectancy >= 0 ? "+" : ""}₩{Math.round(m.expectancy).toLocaleString()}</span>
                )}
                <span>{Math.round(m.match_score * 100)}% match</span>
              </div>
              <p className="coach-rule-match-desc">{m.description}</p>
            </div>
          ))}
        </div>
      )}

      {report.golden_setup_matches.length > 0 && (
        <div className="coach-rule-banner coach-rule-banner-golden">
          <div className="coach-rule-banner-title">
            ✓ Matches your own Golden Setup
          </div>
          {report.golden_setup_matches.map((m) => (
            <div key={m.id} className="coach-rule-match">
              <div className="coach-rule-match-title">{m.title}</div>
              <div className="coach-rule-match-stats">
                {m.win_rate !== null && <span>{(m.win_rate * 100).toFixed(0)}% win rate</span>}
                {m.expectancy !== null && (
                  <span>expectancy {m.expectancy >= 0 ? "+" : ""}₩{Math.round(m.expectancy).toLocaleString()}</span>
                )}
                <span>{Math.round(m.match_score * 100)}% match</span>
              </div>
              <p className="coach-rule-match-desc">{m.description}</p>
            </div>
          ))}
        </div>
      )}

      {report.risk_warnings.length > 0 && (
        <div className="coach-rule-banner coach-rule-banner-risk">
          <div className="coach-rule-banner-title">⚖ Portfolio risk</div>
          <ul className="coach-risk-warning-list">
            {report.risk_warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}

      <div className="coach-align">
        <div className="coach-align-label">
          {retro ? "Quality of the reasoning" : "Alignment with the data"}
        </div>
        <div className={`coach-align-score tone-${tone}`}>
          {score}
          <span className="coach-align-max">/100</span>
        </div>
        <div className="coach-align-bar">
          <div
            className={`coach-align-fill coach-align-fill-${tone}`}
            style={{ width: `${Math.min(100, Math.max(0, score))}%` }}
          />
        </div>
        {retro && (
          <p className="coach-align-hint">
            Scored on what was knowable at the time — before the outcome was
            taken into account.
          </p>
        )}
      </div>

      {/* Never let a confident tone paper over a thin journal. */}
      {!report.history_sufficient && (
        <div className="coach-caution">
          Not enough trade history yet to identify a behavioural pattern — this
          review is based on {retro ? "this trade" : "the current trade"} and the
          available reports only.
        </div>
      )}

      {/* The four-quadrant verdict. Tone follows the process, not the money. */}
      {retro && quadrant && (
        <div className={`coach-quadrant coach-quadrant-${quadrant.tone}`}>
          <div className="coach-quadrant-label">{quadrant.label}</div>
          <p className="coach-quadrant-note">{quadrant.note}</p>
        </div>
      )}

      {retro && report.what_was_knowable && (
        <div className="coach-block">
          <h4 className="coach-block-title">What the data said at the time</h4>
          <p className="coach-text">{report.what_was_knowable}</p>
        </div>
      )}

      <div className="coach-block">
        <h4 className="coach-block-title">
          {retro ? "Your reasoning vs. that data" : "Your rationale vs. the data"}
        </h4>
        <p className="coach-text">{report.rationale_evaluation}</p>
      </div>

      {retro && report.outcome_summary && (
        <div className="coach-block">
          <h4 className="coach-block-title">What happened next</h4>
          <p className="coach-text">{report.outcome_summary}</p>
          {report.hindsight_note && (
            <p className="coach-text coach-hindsight">{report.hindsight_note}</p>
          )}
        </div>
      )}

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
