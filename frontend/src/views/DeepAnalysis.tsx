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
import { AGENT_ORDER, AGENT_NAMES, AGENT_ICONS } from "../components/agentMeta";

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
  const { company, periods } = useDashboard();
  const ticker = company?.ticker ?? null;

  const [startDate, setStartDate] = useState(isoDaysAgo(365));
  const [endDate, setEndDate] = useState(isoDaysAgo(0));
  const [progress, setProgress] = useState<Progress>(IDLE_PROGRESS);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AnalyzeResult | null>(null);
  const [viewingPast, setViewingPast] = useState<AnalysisRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<AnalysisHistoryItem[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const loadHistory = useCallback(async () => {
    if (!ticker) {
      setHistory([]);
      return;
    }
    try {
      const res = await getAnalysisHistory(ticker);
      setHistory(res.history);
    } catch {
      setHistory([]);
    }
  }, [ticker]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // Cancel any in-flight stream on unmount.
  useEffect(() => () => abortRef.current?.abort(), []);

  const handleRun = useCallback(async () => {
    if (running) return;
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
      const res = await runAnalysisStream(startDate, endDate, onEvent, controller.signal);
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
  }, [running, startDate, endDate, loadHistory]);

  const openPastRun = useCallback(async (runId: string) => {
    setError(null);
    try {
      const record = await getAnalysisRun(runId);
      setViewingPast(record);
      setResult(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

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

  const noFilings = periods.length === 0;

  return (
    <div className="view-scroll deep-analysis">
      <div className="view-head">
        <h1 className="view-title">Deep Analysis</h1>
        <p className="view-subtitle">
          Six specialist agents analyze, debate, and synthesize a 3-axis
          (fundamental vs. sentiment vs. price) investment read.
        </p>
      </div>

      <div className="deep-layout">
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
                disabled={running || noFilings}
              >
                {running ? "Running…" : "Run Analysis"}
              </button>
            </div>
            {noFilings ? (
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
                  "The pipeline takes ~1–2 minutes."
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
                  Viewing a past run.{" "}
                  {result && (
                    <button className="link-btn" onClick={() => setViewingPast(null)}>
                      Back to latest
                    </button>
                  )}
                </div>
              )}
              <AnalysisReport
                scores={shown.scores}
                manager={shown.manager}
                reports={shown.reports}
                debate={shown.debate}
                period={shown.period}
                company={shown.company}
              />
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
            History{ticker ? ` · ${ticker}` : ""}
          </h3>
          {!ticker ? (
            <p className="deep-history-empty">
              Past runs appear here once a ticker is identified.
            </p>
          ) : history.length === 0 ? (
            <p className="deep-history-empty">No past runs yet for this company.</p>
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
      </div>
    </div>
  );
}
