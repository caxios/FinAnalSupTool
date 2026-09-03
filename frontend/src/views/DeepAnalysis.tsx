/**
 * views/DeepAnalysis.tsx
 * ──────────────────────
 * View 4 — Deep Analysis (the full Multi-Agent System).
 *
 * Run the six-agent pipeline over a date range and watch it progress live
 * (per-agent, then debate, then synthesis) via the streaming endpoint. The
 * result renders as a 3-axis gap report; past runs for the company are listed in
 * a sidebar and can be reloaded for comparison.
 *
 * The company/ticker come from the uploaded filings (DashboardContext).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type {
  AnalyzeResult,
  AnalyzeProgressEvent,
  AnalysisHistoryItem,
  AnalysisRecord,
  ThreeAxisScores,
} from "../types";
import { useDashboard } from "../context/DashboardContext";
import {
  runAnalysisStream,
  getAnalysisHistory,
  getAnalysisRun,
} from "../api";
import AnalysisReport from "../components/AnalysisReport";
import ResearchPaperView from "../components/analysis/ResearchPaperView";
import ResearchCopilot from "../components/analysis/ResearchCopilot";
import { AGENT_ORDER, AGENT_NAMES, AGENT_ICONS } from "../components/agentMeta";

type ViewMode = "agents" | "paper";

type AgentStatus = "pending" | "ok" | "error" | "skipped";
type Phase = "idle" | "analyzing" | "debating" | "synthesis" | "done";

interface Progress {
  phase: Phase;
  agents: Record<string, AgentStatus>;
  completed: number;
  total: number;
}

const IDLE_PROGRESS: Progress = {
  phase: "idle",
  agents: {},
  completed: 0,
  total: 0,
};

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

const PHASE_STEPS: { key: Phase; label: string }[] = [
  { key: "analyzing", label: "Agents analyzing" },
  { key: "debating", label: "Round-table debate" },
  { key: "synthesis", label: "Manager synthesis" },
];

function PhaseStepper({ phase }: { phase: Phase }) {
  const order: Phase[] = ["analyzing", "debating", "synthesis", "done"];
  const idx = order.indexOf(phase);
  return (
    <div className="phase-stepper">
      {PHASE_STEPS.map((step, i) => {
        const stepIdx = order.indexOf(step.key);
        const state = idx > stepIdx ? "done" : idx === stepIdx ? "active" : "todo";
        return (
          <div key={step.key} className={`phase-step phase-${state}`}>
            <span className="phase-dot">{state === "done" ? "✓" : i + 1}</span>
            <span className="phase-step-label">{step.label}</span>
          </div>
        );
      })}
    </div>
  );
}

function AgentProgress({ progress }: { progress: Progress }) {
  const analyzing = progress.phase === "analyzing";
  return (
    <ul className="agent-progress">
      {AGENT_ORDER.map((id) => {
        const status = progress.agents[id] ?? "pending";
        const working = analyzing && status === "pending";
        return (
          <li key={id} className={`agent-progress-item status-${status}`}>
            <span className="agent-progress-icon">{AGENT_ICONS[id]}</span>
            <span className="agent-progress-name">{AGENT_NAMES[id]}</span>
            <span className="agent-progress-status">
              {status === "ok"
                ? "✓ done"
                : status === "error"
                ? "✕ failed"
                : status === "skipped"
                ? "— skipped"
                : working
                ? "working…"
                : "queued"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function signalChipClass(tone: string | null | undefined): string {
  return `hist-signal signal-${tone === "positive" || tone === "negative" ? tone : "neutral"}`;
}

export default function DeepAnalysis() {
  const { company, periods, activeTicker, setActiveTicker } = useDashboard();
  // The header's selection is authoritative — a company store can exist before
  // its identity resolves from XBRL, and the pipeline is keyed on this ticker.
  const ticker = activeTicker ?? company?.ticker ?? null;

  const [startDate, setStartDate] = useState(isoDaysAgo(365));
  const [endDate, setEndDate] = useState(isoDaysAgo(0));
  const [progress, setProgress] = useState<Progress>(IDLE_PROGRESS);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AnalyzeResult | null>(null);
  const [viewingPast, setViewingPast] = useState<AnalysisRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<AnalysisHistoryItem[]>([]);
  const [viewMode, setViewMode] = useState<ViewMode>("agents");
  const [copilotOpen, setCopilotOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // With a ticker, scope history to that company. Without one (no filings
  // ingested this session and nothing in the portfolio/analysis history to
  // auto-select), fall back to a global feed across every company that has
  // ever been analyzed — that is what lets the sidebar populate before any
  // SEC fetch happens at all.
  const loadHistory = useCallback(async () => {
    try {
      const res = await getAnalysisHistory(ticker ?? undefined, ticker ? 10 : 25);
      setHistory(res.history);
    } catch {
      setHistory([]);
    }
  }, [ticker]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // Switching companies must not leave the previous company's report on
  // screen — EXCEPT when the ticker just changed because openPastRun synced
  // it to match a just-loaded archived record; that record must survive this
  // clear, so it sets skipClearRef to swallow the next run of this effect.
  const skipClearRef = useRef(false);
  useEffect(() => {
    if (skipClearRef.current) {
      skipClearRef.current = false;
      return;
    }
    setResult(null);
    setViewingPast(null);
    setError(null);
    setProgress(IDLE_PROGRESS);
  }, [ticker]);

  // Cancel any in-flight stream on unmount.
  useEffect(() => () => abortRef.current?.abort(), []);

  const handleRun = useCallback(async () => {
    if (running || !ticker) return;
    setError(null);
    setViewingPast(null);
    setResult(null);
    setRunning(true);
    setProgress({
      phase: "analyzing",
      agents: Object.fromEntries(AGENT_ORDER.map((a) => [a, "pending"])) as Record<
        string,
        AgentStatus
      >,
      completed: 0,
      total: AGENT_ORDER.length,
    });

    const controller = new AbortController();
    abortRef.current = controller;

    const onEvent = (ev: AnalyzeProgressEvent) => {
      setProgress((prev) => {
        const next = { ...prev, agents: { ...prev.agents } };
        if (ev.status === "agent_done" && ev.agent) {
          next.agents[ev.agent] = ev.skipped ? "skipped" : ev.ok ? "ok" : "error";
          next.completed = ev.agents_completed ?? next.completed;
          next.total = ev.agents_total ?? next.total;
        } else if (ev.status === "debating") {
          next.phase = "debating";
        } else if (ev.status === "synthesizing") {
          next.phase = "synthesis";
        }
        return next;
      });
    };

    try {
      const res = await runAnalysisStream(
        ticker, startDate, endDate, onEvent, controller.signal
      );
      setResult(res);
      setProgress((prev) => ({ ...prev, phase: "done" }));
      loadHistory();
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setError((err as Error).message);
      }
      setProgress(IDLE_PROGRESS);
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  }, [running, ticker, startDate, endDate, loadHistory]);

  const openPastRun = useCallback(
    async (runId: string) => {
      setError(null);
      try {
        const record = await getAnalysisRun(runId);
        setViewingPast(record);
        setResult(null);
        // A global (multi-company) history list can carry a run from a
        // different company than the one currently in view — sync the shell
        // so the header, sidebar, and this view all agree on which company
        // is being looked at.
        if (record.ticker && record.ticker !== ticker) {
          skipClearRef.current = true;
          setActiveTicker(record.ticker);
        }
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [ticker, setActiveTicker]
  );

  // What to render in the report area: a loaded past run takes precedence.
  const shown:
    | { scores: ThreeAxisScores; manager: AnalysisRecord["manager"]; reports: AnalysisRecord["reports"]; debate: AnalysisRecord["debate"]; period: string; company: string | null }
    | null = viewingPast
    ? {
        scores: viewingPast.three_axis_scores,
        manager: viewingPast.manager,
        reports: viewingPast.reports,
        debate: viewingPast.debate,
        period: viewingPast.analysis_period,
        company: viewingPast.company,
      }
    : result
    ? {
        scores: result.three_axis_scores,
        manager: result.manager,
        reports: result.reports,
        debate: result.debate,
        period: result.analysis_period,
        company: result.company?.name ?? result.company?.ticker ?? null,
      }
    : null;

  // Can't analyze without a company selected, or with no filings for it.
  const noCompany = !ticker;
  const noFilings = periods.length === 0;
  const cannotRun = noCompany || noFilings;

  return (
    <div className="view-scroll deep-analysis">
      <div className="view-head">
        <div>
          <h1 className="view-title">Deep Analysis</h1>
          <p className="view-subtitle">
            Six specialist agents analyze, debate, and synthesize a 3-axis
            (fundamental vs. sentiment vs. price) investment read.
          </p>
        </div>
        <div className="view-head-right">
          <button
            className={`btn-secondary-sm ${copilotOpen ? "is-active" : ""}`}
            onClick={() => setCopilotOpen((v) => !v)}
            title="Ask an ad-hoc, grounded question about this company's data"
          >
            🔎 Data Copilot
          </button>
        </div>
      </div>

      <div className={`deep-layout ${copilotOpen ? "deep-layout-with-copilot" : ""}`}>
        <div className="deep-main">
          {/* Setup panel */}
          <section className="report-card setup-card">
            <div className="setup-row">
              <div className="setup-field">
                <label>Start date</label>
                <input
                  type="date"
                  value={startDate}
                  max={endDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  disabled={running}
                />
              </div>
              <div className="setup-field">
                <label>End date</label>
                <input
                  type="date"
                  value={endDate}
                  min={startDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  disabled={running}
                />
              </div>
              <button
                className="run-btn"
                onClick={handleRun}
                disabled={running || cannotRun}
              >
                {running ? "Running…" : "Run Analysis"}
              </button>
            </div>
            {noCompany ? (
              <p className="setup-note warn">
                Please select a company to analyze — use the switcher in the
                header, or upload a 10-K / 10-Q on the Dashboard.
              </p>
            ) : noFilings ? (
              <p className="setup-note warn">
                Upload a 10-K / 10-Q on the Dashboard first — the analysis is
                anchored to the filed company.
              </p>
            ) : (
              <p className="setup-note">
                {company?.name ? (
                  <>
                    Analyzing <strong>{company.name}</strong>
                    {ticker ? ` (${ticker})` : ""} · the pipeline takes ~1–2
                    minutes.
                  </>
                ) : (
                  <>
                    Analyzing <strong>{ticker}</strong> · the pipeline takes ~1–2
                    minutes.
                  </>
                )}
              </p>
            )}
          </section>

          {/* Progress */}
          {running && (
            <section className="report-card progress-card">
              <PhaseStepper phase={progress.phase} />
              <AgentProgress progress={progress} />
            </section>
          )}

          {error && <div className="report-error-banner">⚠ {error}</div>}

          {/* Result / past run */}
          {shown && (
            <>
              {viewingPast && (
                <div className="viewing-past-bar">
                  <span>
                    Viewing archived analysis from{" "}
                    {viewingPast.timestamp?.slice(0, 10) ?? "an earlier run"}
                    {viewingPast.ticker ? ` for ${viewingPast.ticker}` : ""}.
                  </span>
                  <span className="viewing-past-actions">
                    {result && (
                      <button className="link-btn" onClick={() => setViewingPast(null)}>
                        Back to latest
                      </button>
                    )}
                    {!cannotRun && (
                      <button className="link-btn" onClick={handleRun}>
                        Run Fresh Analysis
                      </button>
                    )}
                    {noFilings && !noCompany && (
                      <Link className="link-btn" to="/">
                        Fetch Fresh SEC
                      </Link>
                    )}
                  </span>
                </div>
              )}
              <div className="view-mode-tabs">
                <button
                  className={`ledger-tab ${viewMode === "agents" ? "is-active" : ""}`}
                  onClick={() => setViewMode("agents")}
                >
                  🧭 Agent View
                </button>
                <button
                  className={`ledger-tab ${viewMode === "paper" ? "is-active" : ""}`}
                  onClick={() => setViewMode("paper")}
                >
                  📄 Research Paper
                </button>
              </div>
              {viewMode === "paper" ? (
                <ResearchPaperView
                  manager={shown.manager}
                  reports={shown.reports}
                  period={shown.period}
                  company={shown.company}
                  ticker={ticker}
                />
              ) : (
                <AnalysisReport
                  scores={shown.scores}
                  manager={shown.manager}
                  reports={shown.reports}
                  debate={shown.debate}
                  period={shown.period}
                  company={shown.company}
                />
              )}
            </>
          )}

          {!shown && !running && !error && (
            <div className="deep-placeholder">
              <span className="deep-placeholder-icon">🧭</span>
              <p>Set a date range and run the analysis to see the full report.</p>
            </div>
          )}
        </div>

        {/* History sidebar */}
        <aside className="deep-history">
          <h3 className="deep-history-title">
            History{ticker ? ` · ${ticker}` : " · All companies"}
          </h3>
          {history.length === 0 ? (
            <p className="deep-history-empty">
              {ticker
                ? "No past runs yet for this company."
                : "No past runs yet. Run Deep Analysis on any company to build a history."}
            </p>
          ) : (
            <ul className="deep-history-list">
              {history.map((h) => (
                <li key={h.run_id}>
                  <button
                    className="history-item"
                    onClick={() => openPastRun(h.run_id)}
                  >
                    <div className="history-top">
                      <span className={signalChipClass(
                        h.overall_signal === "hidden_gem" ||
                          h.overall_signal === "discovery_in_progress" ||
                          h.overall_signal === "consensus_bullish"
                          ? "positive"
                          : h.overall_signal === "overvaluation_warning" ||
                            h.overall_signal === "justified_decline"
                          ? "negative"
                          : "neutral"
                      )}>
                        {h.signal_label ?? h.overall_signal ?? "—"}
                      </span>
                      <span className="history-date">
                        {h.timestamp?.slice(0, 10)}
                      </span>
                    </div>
                    {!ticker && (
                      <div className="history-ticker-badge">
                        {h.ticker ?? "—"}
                        {h.company && h.company !== h.ticker ? ` · ${h.company}` : ""}
                      </div>
                    )}
                    <div className="history-period">{h.analysis_period}</div>
                    <div className="history-scores">
                      <span title="Fundamental">F {h.fundamental_score ?? "—"}</span>
                      <span title="Sentiment">S {h.sentiment_score ?? "—"}</span>
                      <span title="Technical">T {h.technical_score ?? "—"}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        {copilotOpen && (
          <ResearchCopilot ticker={ticker} onClose={() => setCopilotOpen(false)} />
        )}
      </div>
    </div>
  );
}
