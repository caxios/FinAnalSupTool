/**
 * PersonalEdgeDashboard.tsx
 * ───────────────────────────
 * The Personal Trading Edge (🎯) panel — the coach's shift from lecturing to
 * showing the user objective, empirical facts about their OWN past behavior:
 * Expectancy & Payoff Ratio, the Disposition Effect, an MAE/MFE-derived
 * empirical stop-loss, an emotion-vs-PnL breakdown, and a playbook of adopted
 * Golden Setup / Toxic Pattern rules the pre-trade coach checks new trades
 * against.
 *
 * Every number here comes straight from GET /coach/edge-analytics
 * (`services.journal_analysis.edge_analytics`) — this component only lays
 * them out; it computes nothing itself.
 */

import { useCallback, useEffect, useState } from "react";
import type { EdgeAnalytics, ExpectancyStats, RuleCandidate, TradingRule } from "../../types";
import { getEdgeAnalytics, getRules, createRule, setRuleActive, deleteRule } from "../../api";

function pct(n: number | null | undefined, digits = 1): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(digits)}%`;
}

function krw(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}₩${Math.round(n).toLocaleString()}`;
}

function tone(n: number | null | undefined): "positive" | "negative" | "neutral" {
  if (n === null || n === undefined) return "neutral";
  return n > 0 ? "positive" : n < 0 ? "negative" : "neutral";
}

const SEGMENT_LABELS: Record<string, string> = {
  emotional: "Emotional", analytical: "Analytical", mixed: "Mixed",
  unclassified: "Unclassified", none: "No rationale",
  valuation: "Valuation", technical_breakout: "Breakout", dip_buy: "Dip buy",
  momentum: "Momentum",
  calm: "😌 Calm", fomo: "⚡ FOMO", revenge: "🔥 Revenge", boredom: "🥱 Boredom",
  overconfidence: "🚀 Overconfident", fear: "😨 Fear", untagged: "Untagged",
};

function segLabel(key: string): string {
  return SEGMENT_LABELS[key] ?? key;
}

function ExpectancyCard({
  title, value, note,
}: { title: string; value: string; note?: string }) {
  return (
    <div className="edge-kpi-card">
      <div className="edge-kpi-title">{title}</div>
      <div className="edge-kpi-value">{value}</div>
      {note && <div className="edge-kpi-note">{note}</div>}
    </div>
  );
}

/** A row per segment bucket: bar length by |expectancy|, colored win/loss. */
function SegmentBars({ title, segments }: { title: string; segments: Record<string, ExpectancyStats> }) {
  const entries = Object.entries(segments);
  if (entries.length === 0) return null;
  const maxAbs = Math.max(1, ...entries.map(([, s]) => Math.abs(s.expectancy ?? 0)));
  return (
    <div className="edge-segment-block">
      <h4 className="report-block-title">{title}</h4>
      <div className="edge-segment-list">
        {entries
          .sort((a, b) => (b[1].expectancy ?? 0) - (a[1].expectancy ?? 0))
          .map(([key, s]) => (
            <div key={key} className="edge-segment-row">
              <span className="edge-segment-label">{segLabel(key)}</span>
              <div className="attr-bar-track">
                <div
                  className={`attr-bar-fill tone-bg-${tone(s.expectancy)}`}
                  style={{ width: `${Math.min(100, (Math.abs(s.expectancy ?? 0) / maxAbs) * 100)}%` }}
                />
              </div>
              <span className={`edge-segment-value tone-${tone(s.expectancy)}`}>
                {krw(s.expectancy)}
              </span>
              <span className="edge-segment-meta">
                {pct(s.win_rate, 0)} win · n={s.count}
              </span>
            </div>
          ))}
      </div>
    </div>
  );
}

/** Inline SVG scatter of each closed trade's MAE, with the empirical stop-loss threshold marked. */
function MaeMfeChart({ data }: { data: EdgeAnalytics["mae_mfe"] }) {
  const trades = data.trades ?? [];
  if (trades.length === 0) return null;
  const values = trades.map((t) => t.mae);
  const min = Math.min(...values, data.optimal_stop_loss ?? 0);
  const max = Math.max(...values, 0);
  const span = max - min || 1;
  const W = 600, H = 70, PAD = 10;
  const x = (v: number) => PAD + ((v - min) / span) * (W - 2 * PAD);

  return (
    <div className="edge-mae-chart">
      <svg viewBox={`0 0 ${W} ${H}`} className="edge-mae-svg" role="img" aria-label="MAE per closed trade">
        <line x1={x(0)} y1={0} x2={x(0)} y2={H} className="edge-mae-zero-line" />
        {data.optimal_stop_loss !== null && data.optimal_stop_loss !== undefined && (
          <line
            x1={x(data.optimal_stop_loss)} y1={0} x2={x(data.optimal_stop_loss)} y2={H}
            className="edge-mae-threshold-line"
          />
        )}
        {trades.map((t, i) => (
          <circle
            key={i}
            cx={x(t.mae)}
            cy={H / 2 + ((i % 5) - 2) * 10}
            r={5}
            className={`edge-mae-dot ${t.is_win ? "edge-mae-dot-win" : "edge-mae-dot-loss"}`}
          >
            <title>{t.ticker}: MAE {pct(t.mae)}, {t.is_win ? "winner" : "loser"}</title>
          </circle>
        ))}
      </svg>
      <div className="edge-mae-axis">
        <span>{pct(min)}</span>
        <span>0%</span>
        <span>{pct(max)}</span>
      </div>
    </div>
  );
}

function RuleCandidateRow({
  candidate, ruleType, onAdopt, adopting,
}: { candidate: RuleCandidate; ruleType: "golden" | "toxic"; onAdopt: () => void; adopting: boolean }) {
  const c = candidate.conditions;
  const title = `${segLabel(c.rationale_type)} / ${segLabel(c.strategy_type)} / ${segLabel(c.emotion_tag)}`;
  return (
    <div className={`edge-candidate-row edge-candidate-${ruleType}`}>
      <div className="edge-candidate-info">
        <div className="edge-candidate-title">{title}</div>
        <div className="edge-candidate-stats">
          {pct(candidate.win_rate, 0)} win · {krw(candidate.expectancy)} expectancy · n={candidate.count}
        </div>
      </div>
      <button className="btn-secondary-sm" onClick={onAdopt} disabled={adopting}>
        {adopting ? "Adopting…" : "+ Adopt"}
      </button>
    </div>
  );
}

function PlaybookRow({
  rule, onToggle, onDelete,
}: { rule: TradingRule; onToggle: (active: boolean) => void; onDelete: () => void }) {
  return (
    <div className={`edge-playbook-row edge-playbook-${rule.rule_type} ${rule.is_active ? "" : "is-inactive"}`}>
      <label className="edge-toggle">
        <input
          type="checkbox"
          checked={rule.is_active}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span className="edge-toggle-slider" />
      </label>
      <div className="edge-playbook-info">
        <div className="edge-playbook-title">
          <span className={`edge-playbook-badge edge-playbook-badge-${rule.rule_type}`}>
            {rule.rule_type}
          </span>
          {rule.title}
        </div>
        <div className="edge-playbook-desc">{rule.description}</div>
        <div className="edge-playbook-conditions">
          {Object.entries(rule.conditions).map(([k, v]) => (
            <span key={k} className="edge-playbook-chip">{segLabel(v)}</span>
          ))}
          {(rule.win_rate !== null || rule.expectancy !== null) && (
            <span className="edge-playbook-chip edge-playbook-chip-stat">
              {rule.win_rate !== null && `${pct(rule.win_rate, 0)} win`}
              {rule.expectancy !== null && ` · ${krw(rule.expectancy)}`}
            </span>
          )}
        </div>
      </div>
      <button className="btn-remove" title="Remove rule" onClick={onDelete}>✕</button>
    </div>
  );
}

export default function PersonalEdgeDashboard() {
  const [data, setData] = useState<EdgeAnalytics | null>(null);
  const [rules, setRules] = useState<TradingRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adoptingKey, setAdoptingKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [analytics, rulesRes] = await Promise.all([getEdgeAnalytics(), getRules()]);
      setData(analytics);
      setRules(rulesRes.rules);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load edge analytics.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function adopt(ruleType: "golden" | "toxic", candidate: RuleCandidate) {
    const key = `${ruleType}-${candidate.conditions.rationale_type}-${candidate.conditions.strategy_type}-${candidate.conditions.emotion_tag}`;
    setAdoptingKey(key);
    try {
      const c = candidate.conditions;
      await createRule({
        rule_type: ruleType,
        title: `${segLabel(c.rationale_type)} / ${segLabel(c.strategy_type)} / ${segLabel(c.emotion_tag)}`,
        conditions: c,
        description:
          ruleType === "golden"
            ? `Synthesized from your journal: this setup has a ${pct(candidate.win_rate, 0)} win rate and ${krw(candidate.expectancy)} average expectancy across ${candidate.count} trades.`
            : `Synthesized from your journal: this setup has a ${pct(candidate.win_rate, 0)} win rate and ${krw(candidate.expectancy)} average expectancy across ${candidate.count} trades — a pattern to avoid.`,
        win_rate: candidate.win_rate, payoff_ratio: candidate.payoff_ratio,
        expectancy: candidate.expectancy,
      });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to adopt the rule.");
    } finally {
      setAdoptingKey(null);
    }
  }

  async function toggleRule(id: number, active: boolean) {
    setRules((prev) => prev.map((r) => (r.id === id ? { ...r, is_active: active } : r)));
    try {
      await setRuleActive(id, active);
    } catch {
      load(); // resync on failure
    }
  }

  async function removeRule(id: number) {
    setRules((prev) => prev.filter((r) => r.id !== id));
    try {
      await deleteRule(id);
    } catch {
      load();
    }
  }

  if (loading) return <div className="journal-notice">Computing your trading edge…</div>;
  if (error) return <div className="journal-notice journal-error">{error}</div>;
  if (!data) return null;

  if (!data.sufficient) {
    return <div className="journal-notice">{data.note}</div>;
  }

  const disp = data.disposition_effect;
  const mae = data.mae_mfe;

  return (
    <div className="edge-dashboard">
      <p className="edge-dashboard-note">{data.note}</p>

      <div className="edge-kpis">
        <ExpectancyCard title="Win Rate" value={pct(data.overall.win_rate, 0)} />
        <ExpectancyCard
          title="Payoff Ratio"
          value={data.overall.payoff_ratio !== null ? `${data.overall.payoff_ratio.toFixed(2)}x` : "—"}
          note="Avg gain / avg loss"
        />
        <ExpectancyCard
          title="Net Expectancy"
          value={krw(data.overall.expectancy)}
          note="Per closed round trip"
        />
        <ExpectancyCard
          title="Disposition Ratio"
          value={disp.disposition_ratio !== null && disp.disposition_ratio !== undefined ? `${disp.disposition_ratio.toFixed(2)}x` : "—"}
          note={disp.flag ? "⚠ Holding losers too long" : "Losers held vs. winners"}
        />
      </div>

      {/* ── MAE/MFE optimal stop-loss ─────────────────────────── */}
      <div className="edge-section">
        <h3 className="report-block-title">Empirical Stop-Loss (MAE)</h3>
        {mae.optimal_stop_loss_note && (
          <p className="edge-section-note">{mae.optimal_stop_loss_note}</p>
        )}
        {mae.avg_exit_efficiency_note && (
          <p className="edge-section-note">{mae.avg_exit_efficiency_note}</p>
        )}
        <MaeMfeChart data={mae} />
      </div>

      {/* ── Emotion / rationale / strategy breakdown ──────────── */}
      <div className="edge-section">
        <SegmentBars title="Expectancy by Emotion" segments={data.by_emotion_tag} />
        <SegmentBars title="Expectancy by Rationale" segments={data.by_rationale_type} />
        <SegmentBars title="Expectancy by Strategy" segments={data.by_strategy_type} />
      </div>

      {/* ── Rule candidates awaiting adoption ─────────────────── */}
      {(data.rule_candidates.golden_candidates.length > 0 ||
        data.rule_candidates.toxic_candidates.length > 0) && (
        <div className="edge-section">
          <h3 className="report-block-title">Synthesized Candidates</h3>
          <p className="edge-section-note">
            Patterns found in your closed trades. Adopting one makes it active — the
            pre-trade coach will check every new trade against it.
          </p>
          {data.rule_candidates.golden_candidates.map((c, i) => (
            <RuleCandidateRow
              key={`g-${i}`} candidate={c} ruleType="golden"
              onAdopt={() => adopt("golden", c)}
              adopting={adoptingKey === `golden-${c.conditions.rationale_type}-${c.conditions.strategy_type}-${c.conditions.emotion_tag}`}
            />
          ))}
          {data.rule_candidates.toxic_candidates.map((c, i) => (
            <RuleCandidateRow
              key={`t-${i}`} candidate={c} ruleType="toxic"
              onAdopt={() => adopt("toxic", c)}
              adopting={adoptingKey === `toxic-${c.conditions.rationale_type}-${c.conditions.strategy_type}-${c.conditions.emotion_tag}`}
            />
          ))}
        </div>
      )}

      {/* ── My Trading Playbook ────────────────────────────────── */}
      <div className="edge-section">
        <h3 className="report-block-title">My Trading Playbook</h3>
        {rules.length === 0 ? (
          <p className="paper-empty">
            No rules adopted yet. Adopt a synthesized candidate above, or build
            more journal history to unlock one.
          </p>
        ) : (
          <div className="edge-playbook-list">
            {rules.map((r) => (
              <PlaybookRow
                key={r.id} rule={r}
                onToggle={(active) => toggleRule(r.id, active)}
                onDelete={() => removeRule(r.id)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
