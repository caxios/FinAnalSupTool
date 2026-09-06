/**
 * TradingRulesDrawer.tsx
 * ────────────────────────
 * The persistent Investment Rules command center — portfolio UI update plan.
 *
 * One component, three presentation modes (`mode` prop):
 *   - "modal"    — centered overlay, opened instantly from the header, on ANY route.
 *   - "docked"   — a 450px side panel (same layout slot as ChatPanel), for
 *                  continuous side-by-side reference while working elsewhere.
 *   - "embedded" — a plain content block, mounted full-screen inside the
 *                  Portfolio view's "🎯 Investment Rules" sub-tab.
 *
 * Three tabs inside, regardless of mode:
 *   1. Playbook  — established Golden/Toxic/Custom rules, with version badges,
 *      live adherence/violation counts, and a collapsible Evolution Timeline.
 *   2. Evolution Proposals — AI Coach-generated suggestions awaiting approval
 *      (never auto-applied — see `services.rule_evolution`), with a one-click
 *      [Apply Evolution] action and an on-demand "generate now" trigger.
 *   3. Add Custom Rule — a small form to write a rule from scratch, any time.
 */

import { useCallback, useEffect, useState } from "react";
import type {
  RuleEvolutionHistoryItem,
  RuleEvolutionProposal,
  TradingRule,
} from "../../types";
import {
  getRules,
  getRuleHistory,
  getRuleProposals,
  generateRuleProposals,
  applyRuleProposal,
  dismissRuleProposal,
  createRule,
  setRuleActive,
  deleteRule,
} from "../../api";

type Tab = "playbook" | "proposals" | "add";
export type DrawerMode = "modal" | "docked" | "embedded";

interface TradingRulesDrawerProps {
  mode: DrawerMode;
  /** Close the modal, or collapse the docked drawer. Not offered when embedded. */
  onClose?: () => void;
  /** Modal -> docked side panel. Not offered when already docked/embedded. */
  onDock?: () => void;
  /** Fires after any mutation (create/apply/dismiss/toggle/delete) so a
   * parent showing a rule/proposal COUNT badge can refresh it. */
  onChanged?: () => void;
}

function pct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(0)}%`;
}

function krw(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}₩${Math.round(n).toLocaleString()}`;
}

const RULE_TYPE_LABEL: Record<string, string> = {
  golden: "🛡️ Golden Setup", toxic: "⚡ Toxic Pattern", custom: "✏️ Custom",
};

const CHANGE_TYPE_LABEL: Record<string, string> = {
  created: "Created", condition_refined: "Condition refined",
  stats_updated: "Stats updated", risk_tightened: "Risk tightened",
  user_edited: "Edited", deprecated: "Deprecated",
};

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function conditionsText(c: Record<string, string>): string {
  return Object.entries(c)
    .filter(([, v]) => v && v !== "none" && v !== "untagged")
    .map(([k, v]) => `${k.replace("_type", "").replace("_tag", "")}=${v}`)
    .join(", ") || "(no conditions specified)";
}

/** One rule card: version, toggle, stats, and a collapsible Evolution Timeline. */
function RuleCard({
  rule, onToggle, onDelete,
}: {
  rule: TradingRule;
  onToggle: (active: boolean) => void;
  onDelete: () => void;
}) {
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<RuleEvolutionHistoryItem[] | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);

  async function toggleHistory() {
    if (historyOpen) {
      setHistoryOpen(false);
      return;
    }
    setHistoryOpen(true);
    if (history === null) {
      setLoadingHistory(true);
      try {
        const res = await getRuleHistory(rule.id);
        setHistory(res.history);
      } catch {
        setHistory([]);
      } finally {
        setLoadingHistory(false);
      }
    }
  }

  const total = rule.adherence_count + rule.violation_count;
  const adherenceShare = total > 0 ? rule.adherence_count / total : null;

  return (
    <div className={`rule-card rule-card-${rule.rule_type} ${rule.is_active ? "" : "is-inactive"}`}>
      <div className="rule-card-head">
        <label className="edge-toggle">
          <input type="checkbox" checked={rule.is_active} onChange={(e) => onToggle(e.target.checked)} />
          <span className="edge-toggle-slider" />
        </label>
        <span className="rule-card-type">{RULE_TYPE_LABEL[rule.rule_type] ?? rule.rule_type}</span>
        <span className="rule-card-version">v{rule.version}</span>
        <span className="rule-card-title">{rule.title}</span>
        <button className="btn-remove" title="Delete rule" onClick={onDelete}>✕</button>
      </div>
      <p className="rule-card-desc">{rule.description}</p>
      <div className="rule-card-conditions">{conditionsText(rule.conditions)}</div>
      <div className="rule-card-stats">
        {(rule.win_rate !== null || rule.expectancy !== null) && (
          <span className="rule-card-stat">
            {pct(rule.win_rate)} win · {krw(rule.expectancy)} expectancy
          </span>
        )}
        <span className="rule-card-stat" title="Closed round trips currently matching this rule">
          {rule.adherence_count} adherence · {rule.violation_count} violation
          {adherenceShare !== null && ` (${pct(adherenceShare)} adherence rate)`}
        </span>
        {rule.last_evaluated_at && (
          <span className="rule-card-stat rule-card-stat-muted">
            last evaluated {formatWhen(rule.last_evaluated_at)}
          </span>
        )}
      </div>
      {rule.notes && <p className="rule-card-notes">📝 {rule.notes}</p>}

      <button className="btn-secondary-sm rule-history-toggle" onClick={toggleHistory}>
        {historyOpen ? "▾" : "▸"} Evolution Timeline
      </button>
      {historyOpen && (
        <div className="rule-history-list">
          {loadingHistory && <div className="journal-notice">Loading…</div>}
          {history?.length === 0 && <div className="journal-notice">No history yet.</div>}
          {history?.map((h) => (
            <div key={h.id} className="rule-history-item">
              <span className="rule-history-version">v{h.version}</span>
              <span className="rule-history-change">{CHANGE_TYPE_LABEL[h.change_type] ?? h.change_type}</span>
              <span className="rule-history-summary">{h.summary}</span>
              <span className="rule-history-when">{formatWhen(h.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** One pending evolution proposal: rationale, evidence, and Apply/Dismiss. */
function ProposalCard({
  proposal, onApply, onDismiss, busy,
}: {
  proposal: RuleEvolutionProposal;
  onApply: () => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  return (
    <div className={`proposal-card proposal-card-${proposal.rule_type}`}>
      <div className="proposal-card-head">
        <span className="proposal-card-type">{proposal.proposal_type.replace("_", " ")}</span>
        <span className="proposal-card-title">{proposal.title}</span>
      </div>
      <p className="proposal-card-desc">
        <strong>Proposed:</strong> {proposal.description}
      </p>
      <div className="rule-card-conditions">{conditionsText(proposal.conditions)}</div>
      <p className="proposal-card-rationale">💡 {proposal.rationale}</p>
      {proposal.evidence_trade_ids.length > 0 && (
        <p className="proposal-card-evidence">
          Evidence: trade(s) #{proposal.evidence_trade_ids.join(", #")}
        </p>
      )}
      <div className="proposal-card-actions">
        <button className="btn-primary btn-sm" disabled={busy} onClick={onApply}>
          {busy ? "Applying…" : "✅ Apply Evolution"}
        </button>
        <button className="btn-secondary-sm" disabled={busy} onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </div>
  );
}

const ADD_RULE_CONDITION_OPTIONS = {
  rationale_type: ["", "analytical", "emotional", "mixed"],
  strategy_type: ["", "valuation", "technical_breakout", "dip_buy", "momentum"],
  emotion_tag: ["", "calm", "fomo", "revenge", "boredom", "overconfidence", "fear"],
};

function AddRuleForm({ onCreated }: { onCreated: () => void }) {
  const [ruleType, setRuleType] = useState<"golden" | "toxic" | "custom">("custom");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [rationaleType, setRationaleType] = useState("");
  const [strategyType, setStrategyType] = useState("");
  const [emotionTag, setEmotionTag] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !submitting && title.trim() !== "" && description.trim() !== "";

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const conditions: Record<string, string> = {};
      if (rationaleType) conditions.rationale_type = rationaleType;
      if (strategyType) conditions.strategy_type = strategyType;
      if (emotionTag) conditions.emotion_tag = emotionTag;
      await createRule({ rule_type: ruleType, title: title.trim(), description: description.trim(), conditions });
      setTitle("");
      setDescription("");
      setRationaleType("");
      setStrategyType("");
      setEmotionTag("");
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create the rule.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="add-rule-form" onSubmit={handleSubmit}>
      <label className="trade-field">
        <span className="trade-label">Rule Type</span>
        <select className="trade-input" value={ruleType} onChange={(e) => setRuleType(e.target.value as any)}>
          <option value="custom">Custom</option>
          <option value="golden">Golden Setup</option>
          <option value="toxic">Toxic Pattern</option>
        </select>
      </label>
      <label className="trade-field">
        <span className="trade-label">Title</span>
        <input className="trade-input" value={title} onChange={(e) => setTitle(e.target.value)}
               placeholder="e.g. Never chase a breakout already up 15%+" disabled={submitting} />
      </label>
      <label className="trade-field trade-field-rationale">
        <span className="trade-label">Description</span>
        <textarea className="trade-textarea" rows={3} value={description}
                  onChange={(e) => setDescription(e.target.value)} disabled={submitting}
                  placeholder="What the rule says, in your own words." />
      </label>
      <div className="trade-form-row">
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Rationale type</span>
          <select className="trade-input" value={rationaleType} onChange={(e) => setRationaleType(e.target.value)}>
            {ADD_RULE_CONDITION_OPTIONS.rationale_type.map((v) => <option key={v} value={v}>{v || "(any)"}</option>)}
          </select>
        </label>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Strategy type</span>
          <select className="trade-input" value={strategyType} onChange={(e) => setStrategyType(e.target.value)}>
            {ADD_RULE_CONDITION_OPTIONS.strategy_type.map((v) => <option key={v} value={v}>{v || "(any)"}</option>)}
          </select>
        </label>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Emotion tag</span>
          <select className="trade-input" value={emotionTag} onChange={(e) => setEmotionTag(e.target.value)}>
            {ADD_RULE_CONDITION_OPTIONS.emotion_tag.map((v) => <option key={v} value={v}>{v || "(any)"}</option>)}
          </select>
        </label>
      </div>
      {error && <div className="trade-error">{error}</div>}
      <button className="btn-primary" type="submit" disabled={!canSubmit}>
        {submitting ? "Saving…" : "+ Add Rule"}
      </button>
    </form>
  );
}

export default function TradingRulesDrawer({ mode, onClose, onDock, onChanged }: TradingRulesDrawerProps) {
  const [tab, setTab] = useState<Tab>("playbook");
  const [rules, setRules] = useState<TradingRule[]>([]);
  const [proposals, setProposals] = useState<RuleEvolutionProposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [busyProposal, setBusyProposal] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [rulesRes, proposalsRes] = await Promise.all([
        getRules(),
        getRuleProposals("pending"),
      ]);
      setRules(rulesRes.rules);
      setProposals(proposalsRes.proposals);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load rules.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Esc closes a modal (not the docked or embedded presentations).
  useEffect(() => {
    if (mode !== "modal" || !onClose) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [mode, onClose]);

  async function handleToggle(rule: TradingRule, active: boolean) {
    setRules((prev) => prev.map((r) => (r.id === rule.id ? { ...r, is_active: active } : r)));
    try {
      await setRuleActive(rule.id, active);
      onChanged?.();
    } catch {
      load();
    }
  }

  async function handleDelete(rule: TradingRule) {
    setRules((prev) => prev.filter((r) => r.id !== rule.id));
    try {
      await deleteRule(rule.id);
      onChanged?.();
    } catch {
      load();
    }
  }

  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    try {
      await generateRuleProposals({});
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Proposal generation failed.");
    } finally {
      setGenerating(false);
    }
  }

  async function handleApply(proposal: RuleEvolutionProposal) {
    setBusyProposal(proposal.id);
    try {
      await applyRuleProposal(proposal.id);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to apply the proposal.");
    } finally {
      setBusyProposal(null);
    }
  }

  async function handleDismiss(proposal: RuleEvolutionProposal) {
    setBusyProposal(proposal.id);
    try {
      await dismissRuleProposal(proposal.id);
      setProposals((prev) => prev.filter((p) => p.id !== proposal.id));
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to dismiss the proposal.");
    } finally {
      setBusyProposal(null);
    }
  }

  const golden = rules.filter((r) => r.rule_type === "golden");
  const toxic = rules.filter((r) => r.rule_type === "toxic");
  const custom = rules.filter((r) => r.rule_type === "custom");

  const body = (
    <div className="rules-drawer-body">
      <div className="rules-drawer-tabs">
        <button className={`rules-tab ${tab === "playbook" ? "is-active" : ""}`} onClick={() => setTab("playbook")}>
          📖 Playbook ({rules.length})
        </button>
        <button className={`rules-tab ${tab === "proposals" ? "is-active" : ""}`} onClick={() => setTab("proposals")}>
          🧬 Evolution {proposals.length > 0 && <span className="rules-tab-badge">{proposals.length}</span>}
        </button>
        <button className={`rules-tab ${tab === "add" ? "is-active" : ""}`} onClick={() => setTab("add")}>
          + Add Rule
        </button>
      </div>

      {error && <div className="trade-error">{error}</div>}
      {loading ? (
        <div className="journal-notice">Loading your rules…</div>
      ) : (
        <div className="rules-drawer-content">
          {tab === "playbook" && (
            rules.length === 0 ? (
              <p className="paper-empty">
                No rules yet. Adopt a synthesized candidate from Personal Trading
                Edge, or write one under "+ Add Rule".
              </p>
            ) : (
              <>
                {golden.length > 0 && (
                  <div className="rules-section">
                    <h4 className="report-block-title">Golden Setups</h4>
                    {golden.map((r) => (
                      <RuleCard key={r.id} rule={r} onToggle={(a) => handleToggle(r, a)} onDelete={() => handleDelete(r)} />
                    ))}
                  </div>
                )}
                {toxic.length > 0 && (
                  <div className="rules-section">
                    <h4 className="report-block-title">Toxic Patterns</h4>
                    {toxic.map((r) => (
                      <RuleCard key={r.id} rule={r} onToggle={(a) => handleToggle(r, a)} onDelete={() => handleDelete(r)} />
                    ))}
                  </div>
                )}
                {custom.length > 0 && (
                  <div className="rules-section">
                    <h4 className="report-block-title">Custom Rules</h4>
                    {custom.map((r) => (
                      <RuleCard key={r.id} rule={r} onToggle={(a) => handleToggle(r, a)} onDelete={() => handleDelete(r)} />
                    ))}
                  </div>
                )}
              </>
            )
          )}

          {tab === "proposals" && (
            <div className="rules-section">
              <div className="rules-proposals-head">
                <p className="edge-section-note">
                  Generated automatically after a whole-journal review, or on demand.
                  Never applied without your approval.
                </p>
                <button className="btn-secondary-sm" onClick={handleGenerate} disabled={generating}>
                  {generating ? "Analyzing…" : "🔄 Generate Now"}
                </button>
              </div>
              {proposals.length === 0 ? (
                <p className="paper-empty">
                  No pending proposals. Run a whole-journal review, or click
                  "Generate Now" to analyze your recent reviews for evolutions.
                </p>
              ) : (
                proposals.map((p) => (
                  <ProposalCard
                    key={p.id} proposal={p}
                    onApply={() => handleApply(p)}
                    onDismiss={() => handleDismiss(p)}
                    busy={busyProposal === p.id}
                  />
                ))
              )}
            </div>
          )}

          {tab === "add" && <AddRuleForm onCreated={() => { load(); onChanged?.(); }} />}
        </div>
      )}
    </div>
  );

  const header = (
    <div className="rules-drawer-header">
      <h3 className="rules-drawer-title">📜 Investment Rules</h3>
      <div className="rules-drawer-header-actions">
        {mode === "modal" && onDock && (
          <button className="btn-secondary-sm" onClick={onDock} title="Pin to the right side">
            📌 Dock
          </button>
        )}
        {onClose && (
          <button className="btn-close" onClick={onClose} title="Close">✕</button>
        )}
      </div>
    </div>
  );

  if (mode === "embedded") {
    return (
      <div className="rules-drawer rules-drawer-embedded">
        {body}
      </div>
    );
  }

  if (mode === "docked") {
    return (
      <div className="rules-drawer rules-drawer-docked">
        {header}
        {body}
      </div>
    );
  }

  // modal
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="rules-drawer rules-drawer-modal" onClick={(e) => e.stopPropagation()}>
        {header}
        {body}
      </div>
    </div>
  );
}
