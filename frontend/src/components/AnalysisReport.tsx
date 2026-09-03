/**
 * AnalysisReport.tsx
 * ──────────────────
 * Renders a completed MAS analysis: the 3-axis signal + gauges, the Manager's
 * synthesized verdict (recommendation, executive summary, bull/bear case,
 * adjudicated debates, catalysts & risks), expandable per-agent report panels,
 * and the full debate log.
 *
 * Works for both a freshly-run `AnalyzeResult` and a loaded past `AnalysisRecord`
 * (both carry three_axis_scores + manager + reports + debate).
 */

import { useState } from "react";
import type {
  ThreeAxisScores,
  ManagerReport,
  AgentSlot,
  DebateTranscript,
} from "../types";
import ThreeAxisChart from "./ThreeAxisChart";
import DebateLog from "./DebateLog";
import PeerComparisonCard from "./PeerComparisonCard";
import {
  AGENT_ORDER,
  AGENT_NAMES,
  AGENT_ICONS,
  recommendationTone,
} from "./agentMeta";

interface Props {
  scores: ThreeAxisScores;
  manager: (ManagerReport & { error?: string }) | { error: string } | null;
  reports: Record<string, AgentSlot>;
  debate: DebateTranscript | null;
  period?: string;
  company?: string | null;
}

function isManagerReport(
  m: Props["manager"]
): m is ManagerReport & { error?: string } {
  return !!m && "recommendation" in m;
}

/** A titled list of bullet strings; renders nothing when empty. */
function BulletList({ title, items, tone }: { title: string; items: string[]; tone?: string }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="report-block">
      <h4 className={`report-block-title${tone ? ` tone-${tone}` : ""}`}>{title}</h4>
      <ul className="report-list">
        {items.map((it, i) => (
          <li key={i}>{it}</li>
        ))}
      </ul>
    </div>
  );
}

/** One expandable agent report panel. */
function AgentPanel({ agentId, slot }: { agentId: string; slot: AgentSlot }) {
  const name = AGENT_NAMES[agentId] ?? agentId;
  const icon = AGENT_ICONS[agentId] ?? "🔹";
  const failed = typeof slot?.error === "string";
  const confidence = typeof slot.confidence === "number" ? slot.confidence : null;
  const reasoning = typeof slot.reasoning === "string" ? slot.reasoning : null;

  return (
    <details className="agent-panel">
      <summary className="agent-panel-head">
        <span className="agent-panel-icon">{icon}</span>
        <span className="agent-panel-name">{name}</span>
        {failed ? (
          <span className="agent-panel-status status-error">skipped / error</span>
        ) : confidence !== null ? (
          <span className="agent-panel-status">
            confidence {Math.round(confidence * 100)}%
          </span>
        ) : null}
      </summary>
      <div className="agent-panel-body">
        {failed ? (
          <p className="report-error">{String(slot.error)}</p>
        ) : (
          <>
            {reasoning && <p className="agent-reasoning">{reasoning}</p>}
            <details className="agent-raw">
              <summary>View full report (JSON)</summary>
              <pre className="agent-json">{JSON.stringify(slot, null, 2)}</pre>
            </details>
          </>
        )}
      </div>
    </details>
  );
}

export default function AnalysisReport({
  scores,
  manager,
  reports,
  debate,
  period,
  company,
}: Props) {
  const [showDebate, setShowDebate] = useState(false);
  const mgr = isManagerReport(manager) ? manager : null;
  const mgrError =
    manager && !isManagerReport(manager) ? (manager as { error: string }).error : null;

  // Order agent panels by the canonical roster, then any extras. Peer
  // comparison gets its own dedicated card below (a richer render than the
  // generic JSON-dump accordion), so it is excluded here to avoid showing the
  // same report twice.
  const agentIds = [
    ...AGENT_ORDER.filter((a) => a in reports),
    ...Object.keys(reports).filter((a) => !AGENT_ORDER.includes(a)),
  ].filter((a) => a !== "peer_comparison");

  return (
    <div className="analysis-report">
      {(period || company) && (
        <div className="report-context">
          {company && <span className="report-company">{company}</span>}
          {period && <span className="report-period">{period}</span>}
        </div>
      )}

      {/* Signal + 3-axis gauges */}
      <section className="report-card">
        <ThreeAxisChart scores={scores} />
      </section>

      {/* Manager verdict */}
      <section className="report-card">
        <div className="report-card-head">
          <h3>Verdict</h3>
          {mgr && (
            <div className="verdict-badges">
              <span className={`verdict-rec tone-${recommendationTone(mgr.recommendation)}`}>
                {mgr.recommendation}
              </span>
              <span className="verdict-conviction">{mgr.conviction} conviction</span>
              <span className="verdict-score">{mgr.overall_score}/100</span>
            </div>
          )}
        </div>
        {mgrError && <p className="report-error">Manager synthesis failed: {mgrError}</p>}
        {mgr && (
          <>
            <p className="exec-summary">{mgr.executive_summary}</p>
            {mgr.reasoning && <p className="report-reasoning">{mgr.reasoning}</p>}

            <div className="report-two-col">
              <BulletList title="Bull case" items={mgr.bull_case} tone="positive" />
              <BulletList title="Bear case" items={mgr.bear_case} tone="negative" />
            </div>

            {mgr.key_debates && mgr.key_debates.length > 0 && (
              <div className="report-block">
                <h4 className="report-block-title">Key debates, adjudicated</h4>
                <ul className="debate-resolutions">
                  {mgr.key_debates.map((d, i) => (
                    <li key={i} className="debate-resolution">
                      <div className="resolution-topic">{d.topic}</div>
                      <div className="resolution-positions">{d.positions_summary}</div>
                      <div className="resolution-winner">
                        <strong>Stronger side:</strong> {d.winning_side}
                      </div>
                      <div className="resolution-call">
                        <strong>Call:</strong> {d.resolution}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="report-two-col">
              <BulletList
                title="Convergence catalysts / next steps"
                items={mgr.recommended_actions}
              />
              <BulletList title="Risks to thesis" items={mgr.key_risks} tone="negative" />
            </div>

            <BulletList title="Points of consensus" items={mgr.consensus_points} />
          </>
        )}
      </section>

      {/* Peer comparison matrix */}
      <PeerComparisonCard slot={reports.peer_comparison} />

      {/* Per-agent reports */}
      <section className="report-card">
        <div className="report-card-head">
          <h3>Agent reports</h3>
          <span className="report-subtle">{agentIds.length} agents</span>
        </div>
        <div className="agent-panels">
          {agentIds.map((id) => (
            <AgentPanel key={id} agentId={id} slot={reports[id]} />
          ))}
        </div>
      </section>

      {/* Debate log */}
      <section className="report-card">
        <div className="report-card-head">
          <h3>Round-table debate</h3>
          <button className="link-btn" onClick={() => setShowDebate((v) => !v)}>
            {showDebate ? "Hide" : "Show"} transcript
          </button>
        </div>
        {showDebate && <DebateLog debate={debate} />}
      </section>
    </div>
  );
}
