/**
 * ResearchPaperView.tsx
 * ──────────────────────
 * Deep Analysis rendered as a publication-style Institutional Equity Research
 * Paper — six chapters (thesis, business model, industry positioning, quality
 * of earnings forensic, valuation, risks & sensitivities) rather than the
 * per-agent accordion `AnalysisReport` shows.
 *
 * Every figure here already lives on `ManagerReport` (the six chapters) and
 * `reports.sec_filings.quality_of_earnings_forensic` (the raw forensic
 * detail behind chapter 4) — this component only lays them out; it computes
 * and invents nothing.
 */

import type { AgentSlot, ManagerReport, QoEForensicReport } from "../../types";
import { recommendationTone } from "../agentMeta";

interface Props {
  manager: (ManagerReport & { error?: string }) | { error: string } | null;
  reports: Record<string, AgentSlot>;
  period?: string;
  company?: string | null;
  ticker?: string | null;
}

function isManagerReport(
  m: Props["manager"]
): m is ManagerReport & { error?: string } {
  return !!m && "recommendation" in m;
}

function isSecFilingsReport(
  slot: AgentSlot | undefined
): slot is AgentSlot & { quality_of_earnings_forensic: QoEForensicReport; fundamental_score: number } {
  return !!slot && !slot.error && "quality_of_earnings_forensic" in slot;
}

function pct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(1)}%`;
}

function Chapter({
  n, title, children,
}: { n: number; title: string; children: React.ReactNode }) {
  return (
    <details className="paper-chapter" open>
      <summary className="paper-chapter-head">
        <span className="paper-chapter-num">{n}</span>
        <span className="paper-chapter-title">{title}</span>
      </summary>
      <div className="paper-chapter-body">{children}</div>
    </details>
  );
}

function EmptyNote({ text }: { text: string }) {
  return <p className="paper-empty">{text}</p>;
}

/** Builds a self-contained Markdown document from the report, for clipboard/export. */
function buildMarkdown(
  mgr: ManagerReport, sec: (AgentSlot & { quality_of_earnings_forensic: QoEForensicReport }) | null,
  company: string | null | undefined, ticker: string | null | undefined, period: string | undefined,
): string {
  const lines: string[] = [];
  const title = `${company ?? ticker ?? "Company"}${ticker && company ? ` (${ticker})` : ""}`;
  lines.push(`# Institutional Equity Research — ${title}`);
  if (period) lines.push(`*Analysis period: ${period}*`);
  lines.push("");
  lines.push(`**Recommendation:** ${mgr.recommendation.toUpperCase()} · **Conviction:** ${mgr.conviction} · **Score:** ${mgr.overall_score}/100`);
  lines.push("");
  lines.push("## 1. Executive Summary & Thesis");
  lines.push(mgr.executive_summary);
  if (mgr.thesis_pillars.length > 0) {
    lines.push("");
    lines.push("**Core catalyst pillars:**");
    mgr.thesis_pillars.forEach((p) => lines.push(`- ${p}`));
  }
  if (mgr.bull_case.length > 0) {
    lines.push("");
    lines.push("**Bull case:**");
    mgr.bull_case.forEach((p) => lines.push(`- ${p}`));
  }
  if (mgr.bear_case.length > 0) {
    lines.push("");
    lines.push("**Bear case:**");
    mgr.bear_case.forEach((p) => lines.push(`- ${p}`));
  }

  lines.push("");
  lines.push("## 2. Business Model & Segments");
  lines.push(mgr.business_model_and_segments.overview || "(Not available.)");
  if (mgr.business_model_and_segments.segments.length > 0) {
    lines.push("");
    lines.push("| Segment | Revenue | Operating Profit | Notes |");
    lines.push("| --- | --- | --- | --- |");
    mgr.business_model_and_segments.segments.forEach((s) =>
      lines.push(`| ${s.segment} | ${s.revenue_contribution ?? "—"} | ${s.operating_profit_contribution ?? "—"} | ${s.commentary} |`)
    );
  }
  if (mgr.business_model_and_segments.unit_economics_note) {
    lines.push("");
    lines.push(mgr.business_model_and_segments.unit_economics_note);
  }

  lines.push("");
  lines.push("## 3. Industry & Peer Positioning");
  lines.push(mgr.industry_and_peer_positioning.market_structure || "(Not available.)");
  if (mgr.industry_and_peer_positioning.competitive_moat) {
    lines.push("");
    lines.push(`**Competitive moat:** ${mgr.industry_and_peer_positioning.competitive_moat}`);
  }
  if (mgr.industry_and_peer_positioning.peer_multiple_benchmark) {
    lines.push("");
    lines.push(`**Peer multiple benchmark:** ${mgr.industry_and_peer_positioning.peer_multiple_benchmark}`);
  }

  lines.push("");
  lines.push("## 4. Quality of Earnings — Forensic Analysis");
  lines.push(`QoE score: ${mgr.quality_of_earnings_forensic.qoe_score}/100 · Depreciation cliff flagged: ${mgr.quality_of_earnings_forensic.depreciation_cliff_flagged ? "YES" : "no"}`);
  lines.push("");
  lines.push(mgr.quality_of_earnings_forensic.summary || "(Not available.)");
  if (mgr.quality_of_earnings_forensic.structural_vs_transitory_verdict) {
    lines.push("");
    lines.push(`**Structural vs. transitory verdict:** ${mgr.quality_of_earnings_forensic.structural_vs_transitory_verdict}`);
  }
  if (sec) {
    lines.push("");
    lines.push("**Sloan accrual table (computed):**");
    lines.push("");
    lines.push("| Period | Net Income | OCF | Sloan Accrual | Flag | Cash Conversion |");
    lines.push("| --- | --- | --- | --- | --- | --- |");
    sec.quality_of_earnings_forensic.accrual_table.forEach((r) =>
      lines.push(`| ${r.period} | ${r.net_income ?? "—"} | ${r.operating_cash_flow ?? "—"} | ${r.sloan_accrual_ratio ?? "—"} | ${r.accrual_flag ?? "—"} | ${r.cash_conversion_ratio ?? "—"} |`)
    );
    if (sec.quality_of_earnings_forensic.capex_da_reconciliation) {
      lines.push("");
      lines.push(`**CapEx/D&A reconciliation:** ${sec.quality_of_earnings_forensic.capex_da_reconciliation}`);
    }
    if (sec.quality_of_earnings_forensic.structural_drivers.length > 0) {
      lines.push("");
      lines.push("**Structural drivers:**");
      sec.quality_of_earnings_forensic.structural_drivers.forEach((d) => lines.push(`- ${d}`));
    }
    if (sec.quality_of_earnings_forensic.transitory_drivers.length > 0) {
      lines.push("");
      lines.push("**Transitory drivers:**");
      sec.quality_of_earnings_forensic.transitory_drivers.forEach((d) => lines.push(`- ${d}`));
    }
  }

  lines.push("");
  lines.push("## 5. Valuation Thesis");
  lines.push(mgr.valuation_thesis.peer_relative_read || "(Not available.)");
  if (mgr.valuation_thesis.target_valuation_band) {
    lines.push("");
    lines.push(`**Target valuation band:** ${mgr.valuation_thesis.target_valuation_band}`);
  }
  if (mgr.valuation_thesis.scenarios.length > 0) {
    lines.push("");
    lines.push("| Scenario | Assumption | Implied value | Basis |");
    lines.push("| --- | --- | --- | --- |");
    mgr.valuation_thesis.scenarios.forEach((s) =>
      lines.push(`| ${s.scenario} | ${s.assumption} | ${s.implied_value ?? "—"} | ${s.basis ?? "—"} |`)
    );
  }

  lines.push("");
  lines.push("## 6. Key Risks & Sensitivities");
  if (mgr.key_risks.length > 0) {
    mgr.key_risks.forEach((r) => lines.push(`- ${r}`));
  }
  const rs = mgr.key_risks_and_sensitivities;
  [
    ["Macro sensitivity", rs.macro_sensitivity],
    ["Supply chain risk", rs.supply_chain_risk],
    ["Regulatory headwinds", rs.regulatory_headwinds],
  ].forEach(([label, val]) => {
    if (val) lines.push(`- **${label}:** ${val}`);
  });

  return lines.join("\n");
}

export default function ResearchPaperView({ manager, reports, period, company, ticker }: Props) {
  const mgr = isManagerReport(manager) ? manager : null;
  const mgrError =
    manager && !isManagerReport(manager) ? (manager as { error: string }).error : null;
  const secSlot = reports.sec_filings;
  const sec = isSecFilingsReport(secSlot) ? secSlot : null;

  if (mgrError) {
    return <div className="report-error-banner">⚠ Manager synthesis failed: {mgrError}</div>;
  }
  if (!mgr) {
    return <div className="deep-placeholder"><p>Run an analysis to generate the research paper.</p></div>;
  }

  const copyMarkdown = async () => {
    const md = buildMarkdown(mgr, sec, company, ticker, period);
    try {
      await navigator.clipboard.writeText(md);
    } catch {
      // Clipboard permission denied — nothing more this component can do.
    }
  };

  const printPaper = () => {
    const md = buildMarkdown(mgr, sec, company, ticker, period);
    const win = window.open("", "_blank");
    if (!win) return;
    const escaped = md
      .split("\n")
      .map((line) => {
        if (line.startsWith("# ")) return `<h1>${line.slice(2)}</h1>`;
        if (line.startsWith("## ")) return `<h2>${line.slice(3)}</h2>`;
        if (line.startsWith("| ")) return `<div class="paper-print-row">${line}</div>`;
        if (line.startsWith("- ")) return `<li>${line.slice(2)}</li>`;
        if (line.startsWith("*") && line.endsWith("*") && line.length > 1) {
          return `<p class="paper-print-italic">${line.slice(1, -1)}</p>`;
        }
        return line.trim() ? `<p>${line}</p>` : "";
      })
      .join("\n");
    win.document.write(
      `<!doctype html><html><head><title>${(company ?? ticker ?? "Research Paper")} — Equity Research</title>` +
      `<style>
        body { font-family: Georgia, 'Times New Roman', serif; max-width: 780px; margin: 40px auto; color: #1a1a1a; line-height: 1.6; padding: 0 20px; }
        h1 { font-size: 24px; border-bottom: 2px solid #1a1a1a; padding-bottom: 8px; }
        h2 { font-size: 17px; margin-top: 28px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }
        p { font-size: 13px; margin: 6px 0; }
        li { font-size: 13px; margin-left: 20px; }
        .paper-print-row { font-family: 'Courier New', monospace; font-size: 11px; white-space: pre; }
        .paper-print-italic { font-style: italic; color: #555; }
        @media print { body { margin: 0; } }
      </style></head><body>${escaped}</body></html>`
    );
    win.document.close();
    win.focus();
    win.print();
  };

  return (
    <div className="research-paper">
      <div className="paper-toolbar">
        <div className="paper-masthead">
          <span className="paper-masthead-title">Institutional Equity Research</span>
          {company && <span className="paper-masthead-company">{company}{ticker ? ` (${ticker})` : ""}</span>}
          {period && <span className="paper-masthead-period">{period}</span>}
        </div>
        <div className="paper-actions">
          <button className="btn-secondary-sm" onClick={copyMarkdown}>📋 Copy as Markdown</button>
          <button className="btn-secondary-sm" onClick={printPaper}>🖨️ Print / Export PDF</button>
        </div>
      </div>

      <div className="paper-verdict-strip">
        <span className={`verdict-rec tone-${recommendationTone(mgr.recommendation)}`}>{mgr.recommendation}</span>
        <span className="verdict-conviction">{mgr.conviction} conviction</span>
        <span className="verdict-score">{mgr.overall_score}/100</span>
      </div>

      <Chapter n={1} title="Executive Summary & Investment Thesis">
        <p className="paper-lead">{mgr.executive_summary}</p>
        {mgr.thesis_pillars.length > 0 && (
          <>
            <h4 className="report-block-title">Core catalyst pillars</h4>
            <ul className="report-list">
              {mgr.thesis_pillars.map((p, i) => <li key={i}>{p}</li>)}
            </ul>
          </>
        )}
        <div className="report-two-col">
          {mgr.bull_case.length > 0 && (
            <div className="report-block">
              <h4 className="report-block-title tone-positive">Bull case</h4>
              <ul className="report-list">{mgr.bull_case.map((p, i) => <li key={i}>{p}</li>)}</ul>
            </div>
          )}
          {mgr.bear_case.length > 0 && (
            <div className="report-block">
              <h4 className="report-block-title tone-negative">Bear case</h4>
              <ul className="report-list">{mgr.bear_case.map((p, i) => <li key={i}>{p}</li>)}</ul>
            </div>
          )}
        </div>
      </Chapter>

      <Chapter n={2} title="Business Model & Segments">
        {mgr.business_model_and_segments.overview
          ? <p className="paper-lead">{mgr.business_model_and_segments.overview}</p>
          : <EmptyNote text="No business-model breakdown was available from this run." />}
        {mgr.business_model_and_segments.segments.length > 0 && (
          <div className="table-wrapper">
            <table className="fin-table">
              <thead><tr><th>Segment</th><th>Revenue</th><th>Operating profit</th><th>Commentary</th></tr></thead>
              <tbody>
                {mgr.business_model_and_segments.segments.map((s, i) => (
                  <tr key={i}>
                    <td>{s.segment}</td>
                    <td>{s.revenue_contribution ?? "—"}</td>
                    <td>{s.operating_profit_contribution ?? "—"}</td>
                    <td>{s.commentary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {mgr.business_model_and_segments.unit_economics_note && (
          <p className="agent-reasoning">{mgr.business_model_and_segments.unit_economics_note}</p>
        )}
      </Chapter>

      <Chapter n={3} title="Industry & Peer Positioning">
        {mgr.industry_and_peer_positioning.market_structure
          ? <p className="paper-lead">{mgr.industry_and_peer_positioning.market_structure}</p>
          : <EmptyNote text="No industry positioning was available from this run." />}
        {mgr.industry_and_peer_positioning.competitive_moat && (
          <div className="report-block">
            <h4 className="report-block-title">Competitive moat</h4>
            <p className="agent-reasoning">{mgr.industry_and_peer_positioning.competitive_moat}</p>
          </div>
        )}
        {mgr.industry_and_peer_positioning.peer_multiple_benchmark && (
          <div className="report-block">
            <h4 className="report-block-title">Peer multiple benchmark</h4>
            <p className="agent-reasoning">{mgr.industry_and_peer_positioning.peer_multiple_benchmark}</p>
          </div>
        )}
      </Chapter>

      <Chapter n={4} title="Quality of Earnings — Forensic Analysis">
        <div className="paper-qoe-box">
          <div className="paper-qoe-head">
            <span className="paper-qoe-score">QoE {mgr.quality_of_earnings_forensic.qoe_score}/100</span>
            {mgr.quality_of_earnings_forensic.depreciation_cliff_flagged && (
              <span className="paper-qoe-flag">⚠ Depreciation cliff flagged</span>
            )}
          </div>
          <p className="paper-lead">
            {mgr.quality_of_earnings_forensic.summary || "No forensic QoE synthesis was available from this run."}
          </p>
          {mgr.quality_of_earnings_forensic.structural_vs_transitory_verdict && (
            <p className="agent-reasoning">
              <strong>Verdict:</strong> {mgr.quality_of_earnings_forensic.structural_vs_transitory_verdict}
            </p>
          )}
        </div>

        {sec ? (
          <>
            {sec.quality_of_earnings_forensic.accrual_table.length > 0 && (
              <div className="table-wrapper">
                <table className="fin-table">
                  <thead>
                    <tr><th>Period</th><th>Net Income</th><th>OCF</th><th>Sloan Accrual</th><th>Flag</th><th>Cash Conv.</th></tr>
                  </thead>
                  <tbody>
                    {sec.quality_of_earnings_forensic.accrual_table.map((r) => (
                      <tr key={r.period}>
                        <td>{r.period}</td>
                        <td>{r.net_income ?? "—"}</td>
                        <td>{r.operating_cash_flow ?? "—"}</td>
                        <td className={`tone-${r.accrual_flag === "aggressive" ? "negative" : "neutral"}`}>
                          {r.sloan_accrual_ratio ?? "—"}
                        </td>
                        <td>{r.accrual_flag ?? "—"}</td>
                        <td>{r.cash_conversion_ratio ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {sec.quality_of_earnings_forensic.capex_da_reconciliation && (
              <div className="report-block">
                <h4 className="report-block-title">CapEx / D&amp;A reconciliation</h4>
                <p className="agent-reasoning">{sec.quality_of_earnings_forensic.capex_da_reconciliation}</p>
              </div>
            )}
            {sec.quality_of_earnings_forensic.depreciation_cliff_note && (
              <div className="report-block">
                <h4 className="report-block-title">Depreciation cliff evidence</h4>
                <p className="agent-reasoning">{sec.quality_of_earnings_forensic.depreciation_cliff_note}</p>
              </div>
            )}
            <div className="report-two-col">
              {sec.quality_of_earnings_forensic.structural_drivers.length > 0 && (
                <div className="report-block">
                  <h4 className="report-block-title tone-positive">Structural drivers</h4>
                  <ul className="report-list">
                    {sec.quality_of_earnings_forensic.structural_drivers.map((d, i) => <li key={i}>{d}</li>)}
                  </ul>
                </div>
              )}
              {sec.quality_of_earnings_forensic.transitory_drivers.length > 0 && (
                <div className="report-block">
                  <h4 className="report-block-title tone-negative">Transitory / accounting drivers</h4>
                  <ul className="report-list">
                    {sec.quality_of_earnings_forensic.transitory_drivers.map((d, i) => <li key={i}>{d}</li>)}
                  </ul>
                </div>
              )}
            </div>
            {sec.quality_of_earnings_forensic.footnote_crossmatch.length > 0 && (
              <div className="report-block">
                <h4 className="report-block-title">Footnote cross-matches</h4>
                <ul className="report-list">
                  {sec.quality_of_earnings_forensic.footnote_crossmatch.map((d, i) => <li key={i}>{d}</li>)}
                </ul>
              </div>
            )}
          </>
        ) : (
          <EmptyNote text="The SEC Filings agent did not report on this run, so no accrual table or driver breakdown is available." />
        )}
      </Chapter>

      <Chapter n={5} title="Valuation Thesis">
        {mgr.valuation_thesis.peer_relative_read
          ? <p className="paper-lead">{mgr.valuation_thesis.peer_relative_read}</p>
          : <EmptyNote text="No peer-relative valuation read was available from this run." />}
        {mgr.valuation_thesis.target_valuation_band && (
          <p className="agent-reasoning">
            <strong>Target valuation band:</strong> {mgr.valuation_thesis.target_valuation_band}
          </p>
        )}
        {mgr.valuation_thesis.scenarios.length > 0 && (
          <div className="table-wrapper">
            <table className="fin-table">
              <thead><tr><th>Scenario</th><th>Assumption</th><th>Implied value</th><th>Basis</th></tr></thead>
              <tbody>
                {mgr.valuation_thesis.scenarios.map((s, i) => (
                  <tr key={i}>
                    <td className={`tone-${s.scenario === "bull" ? "positive" : s.scenario === "bear" ? "negative" : "neutral"}`}>
                      {s.scenario}
                    </td>
                    <td>{s.assumption}</td>
                    <td>{s.implied_value ?? "—"}</td>
                    <td>{s.basis ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Chapter>

      <Chapter n={6} title="Key Risks & Sensitivities">
        {mgr.key_risks.length > 0 && (
          <ul className="report-list">{mgr.key_risks.map((r, i) => <li key={i}>{r}</li>)}</ul>
        )}
        <div className="report-two-col">
          {mgr.key_risks_and_sensitivities.macro_sensitivity && (
            <div className="report-block">
              <h4 className="report-block-title">Macro sensitivity</h4>
              <p className="agent-reasoning">{mgr.key_risks_and_sensitivities.macro_sensitivity}</p>
            </div>
          )}
          {mgr.key_risks_and_sensitivities.supply_chain_risk && (
            <div className="report-block">
              <h4 className="report-block-title">Supply chain risk</h4>
              <p className="agent-reasoning">{mgr.key_risks_and_sensitivities.supply_chain_risk}</p>
            </div>
          )}
          {mgr.key_risks_and_sensitivities.regulatory_headwinds && (
            <div className="report-block">
              <h4 className="report-block-title">Regulatory headwinds</h4>
              <p className="agent-reasoning">{mgr.key_risks_and_sensitivities.regulatory_headwinds}</p>
            </div>
          )}
        </div>
        {mgr.key_risks_and_sensitivities.sensitivities.length > 0 && (
          <div className="table-wrapper">
            <table className="fin-table">
              <thead><tr><th>Factor</th><th>Sensitivity</th></tr></thead>
              <tbody>
                {mgr.key_risks_and_sensitivities.sensitivities.map((s, i) => (
                  <tr key={i}><td>{s.factor}</td><td>{s.sensitivity}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Chapter>
    </div>
  );
}
