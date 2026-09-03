/**
 * PeerComparisonCard.tsx
 * ───────────────────────
 * The Peer Comparison Matrix: target vs. real, named industry peers across
 * valuation, profitability, growth, and financial health.
 *
 * Every number here comes straight from `peer_comparison`'s `metrics_table`
 * (computed in `providers.peer_provider`, never by the LLM) — this component
 * only formats and colors what the backend already decided. `higher_is_better`
 * drives which direction a green/red badge means: a low P/E is cheap (good), a
 * high margin is strong (good).
 */

import type { AgentSlot, PeerComparisonReport, PeerMetricRow } from "../types";

const PERCENT_METRICS = new Set([
  "gross_margin", "operating_margin", "net_margin", "roe", "roic_proxy",
  "revenue_growth", "earnings_growth", "fcf_margin",
]);
const MULTIPLE_METRICS = new Set([
  "trailing_pe", "forward_pe", "ev_ebitda", "price_to_sales", "price_to_book", "peg_ratio",
]);

const GROUPS: { title: string; metrics: string[] }[] = [
  {
    title: "Valuation",
    metrics: ["trailing_pe", "forward_pe", "ev_ebitda", "price_to_sales", "price_to_book", "peg_ratio"],
  },
  {
    title: "Profitability",
    metrics: ["gross_margin", "operating_margin", "net_margin", "roe", "roic_proxy"],
  },
  { title: "Growth", metrics: ["revenue_growth", "earnings_growth"] },
  { title: "Financial Health", metrics: ["debt_to_equity", "current_ratio", "fcf_margin"] },
];

function formatValue(metric: string, v: number | null): string {
  if (v === null || v === undefined) return "—";
  if (PERCENT_METRICS.has(metric)) return `${(v * 100).toFixed(1)}%`;
  if (MULTIPLE_METRICS.has(metric)) return `${v.toFixed(2)}x`;
  return v.toFixed(2);
}

function assessmentTone(a: string): "positive" | "negative" | "neutral" {
  if (a === "discount") return "positive";
  if (a === "premium") return "neutral";
  return "neutral";
}

/** green when this row favors the target, red when it favors peers, gray inside a small dead zone. */
function rowTone(row: PeerMetricRow): "positive" | "negative" | "neutral" {
  const pct = row.premium_discount_pct;
  if (pct === null || pct === undefined || Math.abs(pct) < 0.05) return "neutral";
  const targetIsHigher = pct > 0;
  const favorsTarget = row.higher_is_better ? targetIsHigher : !targetIsHigher;
  return favorsTarget ? "positive" : "negative";
}

function isPeerReport(slot: AgentSlot | undefined): slot is PeerComparisonReport {
  return !!slot && !slot.error && Array.isArray((slot as PeerComparisonReport).metrics_table);
}

export default function PeerComparisonCard({ slot }: { slot: AgentSlot | undefined }) {
  if (!isPeerReport(slot)) return null;
  const report = slot;
  const byMetric = new Map(report.metrics_table.map((r) => [r.metric, r]));

  return (
    <section className="report-card peer-card">
      <div className="report-card-head">
        <h3>Peer Comparison — {report.target_ticker}</h3>
        <span className={`peer-assessment tone-${assessmentTone(report.valuation_assessment)}`}>
          {report.valuation_assessment === "in_line"
            ? "in line with peers"
            : `trading at a ${report.valuation_assessment}`}
        </span>
      </div>

      <div className="peer-meta">
        {report.sector && <span>{report.sector}</span>}
        {report.industry && <span>{report.industry}</span>}
        <span>
          vs. {report.peer_tickers.length > 0 ? report.peer_tickers.join(", ") : "no identified peers"}
        </span>
      </div>

      {report.reasoning && <p className="agent-reasoning">{report.reasoning}</p>}

      {report.metrics_table.length > 0 && (
        <div className="peer-groups">
          {GROUPS.map((group) => {
            const rows = group.metrics
              .map((m) => byMetric.get(m))
              .filter((r): r is PeerMetricRow => !!r);
            if (rows.length === 0) return null;
            return (
              <div key={group.title} className="peer-group">
                <h4 className="report-block-title">{group.title}</h4>
                <div className="table-wrapper">
                  <table className="peer-table">
                    <thead>
                      <tr>
                        <th>Metric</th>
                        <th>{report.target_ticker}</th>
                        <th>Peer median</th>
                        <th>Peer range</th>
                        <th>vs. median</th>
                        <th>Percentile</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <tr key={row.metric}>
                          <td>{row.label}</td>
                          <td className={`tone-${rowTone(row)} peer-target-cell`}>
                            {formatValue(row.metric, row.target_value)}
                          </td>
                          <td>{formatValue(row.metric, row.peer_median)}</td>
                          <td className="peer-range">
                            {formatValue(row.metric, row.peer_min)} – {formatValue(row.metric, row.peer_max)}
                          </td>
                          <td className={`tone-${rowTone(row)}`}>
                            {row.premium_discount_pct === null
                              ? "—"
                              : `${row.premium_discount_pct > 0 ? "+" : ""}${(row.premium_discount_pct * 100).toFixed(1)}%`}
                          </td>
                          <td>{row.percentile === null ? "—" : `${row.percentile}th`}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {report.competitive_moat && (
        <div className="report-block">
          <h4 className="report-block-title">Competitive moat</h4>
          <p className="agent-reasoning">{report.competitive_moat}</p>
        </div>
      )}

      {report.key_differentiators.length > 0 && (
        <div className="report-block">
          <h4 className="report-block-title">Key differentiators</h4>
          <ul className="report-list">
            {report.key_differentiators.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        </div>
      )}

      {(report.excluded_peers.length > 0 || report.data_limitations.length > 0) && (
        <p className="peer-footnote">
          {report.excluded_peers.length > 0 &&
            `Excluded (no usable data): ${report.excluded_peers.join(", ")}. `}
          {report.data_limitations.join(" ")}
        </p>
      )}
    </section>
  );
}
