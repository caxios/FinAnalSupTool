/**
 * DebateLog.tsx
 * ─────────────
 * Renders the round-table debate transcript: each agent's turn, its stance, the
 * argument, and the specific evidence it cited from its raw data. Turns appear
 * in speaking order so the back-and-forth (refutations, agreements) reads
 * naturally.
 */

import type { DebateTranscript } from "../types";
import { AGENT_NAMES, stanceTone } from "./agentMeta";

export default function DebateLog({ debate }: { debate: DebateTranscript | null }) {
  if (!debate || debate.history.length === 0) {
    return (
      <p className="report-empty">
        No debate was held — it needs at least two agents to produce reports.
      </p>
    );
  }

  return (
    <div className="debate-log">
      <div className="debate-meta">
        {debate.history.length} arguments over {debate.rounds} round
        {debate.rounds === 1 ? "" : "s"} ·{" "}
        {debate.consensus_reached ? "consensus reached" : "no consensus"}
      </div>
      <ol className="debate-turns">
        {debate.history.map((turn, i) => (
          <li key={i} className="debate-turn">
            <div className="debate-turn-head">
              <span className="debate-agent">
                {AGENT_NAMES[turn.agent_id] ?? turn.agent_id}
              </span>
              <span className={`stance-tag stance-${stanceTone(turn.stance)}`}>
                {turn.stance}
              </span>
            </div>
            <p className="debate-argument">{turn.argument}</p>
            {turn.cited_evidence.length > 0 && (
              <ul className="debate-evidence">
                {turn.cited_evidence.map((ev, j) => (
                  <li key={j}>{ev}</li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
