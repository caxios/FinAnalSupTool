/**
 * ResearchCopilot.tsx
 * ────────────────────
 * On-demand data extraction drawer for the Deep Analysis workspace. An analyst
 * drafting a research note asks a specific question — a preset quick query or
 * a custom prompt — and gets back a grounded Markdown table (when
 * applicable), citations, and a short analytical note, without leaving the
 * page. Backed by POST /analysis/query-data (`services.research_copilot`),
 * which extracts from already-available data rather than inventing anything.
 */

import { useState } from "react";
import type { QueryDataResponse, QueryDataScope } from "../../types";
import { queryResearchData } from "../../api";

const PRESETS: { label: string; query: string; scope: QueryDataScope }[] = [
  { label: "3-Year Segment Revenue Table", query: "Build a table of segment/product-line revenue for every period available.", scope: "financials" },
  { label: "CapEx vs D&A Trend", query: "Show CapEx and Depreciation & Amortization for every period, and note the trend.", scope: "financials" },
  { label: "MD&A on Operating Margin Drivers", query: "What does the MD&A say about what drove operating margin changes?", scope: "sec_text" },
  { label: "Peer Multiple Comparison", query: "Compare this company's valuation multiples (P/E, EV/EBITDA, P/S) against its peers.", scope: "peers" },
];

const SCOPE_LABELS: Record<QueryDataScope, string> = {
  financials: "Financial tables",
  sec_text: "Filing text (MD&A / footnotes / risk factors)",
  earnings: "Earnings call (last analysis run)",
  peers: "Live peer metrics",
  all: "Everything available",
};

function copy(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

export default function ResearchCopilot({
  ticker, onClose,
}: { ticker: string | null; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<QueryDataScope>("all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<QueryDataResponse | null>(null);

  async function run(q: string, s: QueryDataScope) {
    if (!ticker || !q.trim() || loading) return;
    setQuery(q);
    setScope(s);
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await queryResearchData(ticker, q, s));
    } catch (err) {
      setError(err instanceof Error ? err.message : "The query failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <aside className="copilot-drawer">
      <div className="copilot-head">
        <h3>🔎 Research Data Copilot</h3>
        <button className="chat-close" onClick={onClose} title="Close">✕</button>
      </div>

      {!ticker ? (
        <p className="deep-history-empty">Select a company to query its data.</p>
      ) : (
        <>
          <div className="copilot-presets">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                className="copilot-preset-btn"
                disabled={loading}
                onClick={() => run(p.query, p.scope)}
              >
                {p.label}
              </button>
            ))}
          </div>

          <form
            className="copilot-form"
            onSubmit={(e) => {
              e.preventDefault();
              run(query, scope);
            }}
          >
            <textarea
              className="copilot-input"
              placeholder="Ask a specific question, e.g. 'Extract the interest coverage ratio for every period.'"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              disabled={loading}
              rows={3}
            />
            <div className="copilot-form-row">
              <select
                className="copilot-scope-select"
                value={scope}
                onChange={(e) => setScope(e.target.value as QueryDataScope)}
                disabled={loading}
              >
                {(Object.keys(SCOPE_LABELS) as QueryDataScope[]).map((s) => (
                  <option key={s} value={s}>{SCOPE_LABELS[s]}</option>
                ))}
              </select>
              <button className="btn-primary" type="submit" disabled={loading || !query.trim()}>
                {loading ? "Querying…" : "Ask"}
              </button>
            </div>
          </form>

          {error && <div className="journal-notice journal-error">{error}</div>}

          {result && (
            <div className="copilot-result">
              {result.table_markdown && (
                <div className="copilot-result-block">
                  <div className="copilot-result-head">
                    <span>Table</span>
                    <button className="link-btn" onClick={() => copy(result.table_markdown ?? "")}>
                      Copy Markdown
                    </button>
                  </div>
                  <pre className="copilot-table-markdown">{result.table_markdown}</pre>
                </div>
              )}

              {result.analytical_note && (
                <div className="copilot-result-block">
                  <div className="copilot-result-head">
                    <span>Analytical note</span>
                    <button className="link-btn" onClick={() => copy(result.analytical_note)}>
                      Copy
                    </button>
                  </div>
                  <p className="agent-reasoning">{result.analytical_note}</p>
                </div>
              )}

              {result.citations.length > 0 && (
                <div className="copilot-result-block">
                  <div className="copilot-result-head"><span>Citations</span></div>
                  <ul className="copilot-citations">
                    {result.citations.map((c, i) => (
                      <li key={i} className="copilot-citation">
                        <div className="copilot-citation-meta">
                          <strong>{c.period}</strong> — {c.section}
                        </div>
                        <div className="copilot-citation-excerpt">&ldquo;{c.excerpt}&rdquo;</div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {!result.table_markdown && !result.analytical_note && result.citations.length === 0 && (
                <p className="deep-history-empty">No grounded answer was found for this question.</p>
              )}
            </div>
          )}
        </>
      )}
    </aside>
  );
}
