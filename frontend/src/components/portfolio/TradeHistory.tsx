/**
 * TradeHistory.tsx
 * ────────────────
 * The trading journal: every logged trade in reverse-chronological order, with
 * the user's own **entry rationale** shown in full alongside the numbers.
 *
 * The rationale is not truncated to a tooltip. Reading back what you were
 * thinking at the time — next to what the trade actually cost — is the whole
 * point of keeping the journal, and it is the raw material the Coach agent
 * works from.
 *
 * The journal is also where coaching is *reached*. It used to be available only
 * from the trade form, before "Log trade" was pressed, which meant any rationale
 * written in a hurry got no feedback ever. Every row here can be reviewed after
 * the fact, and the banner at the top counts the ones that never were.
 *
 * Paging reuses `usePagination` + the shared `Pagination` component, same as the
 * media feeds.
 */

import { useCallback, useEffect, useState } from "react";
import type { StoredReview, Trade, CoachReport } from "../../types";
import { usePagination } from "../../hooks/usePagination";
import Pagination from "../media/Pagination";
import CoachReview from "./CoachReview";
import { getReviews, getPendingReviews, reviewLoggedTrade } from "../../api";

/** Marker the backend writes for a position seeded at portfolio setup. */
const OPENING_RATIONALE = "Opening position recorded at portfolio setup.";

interface TradeHistoryProps {
  trades: Trade[];
  loading: boolean;
  error: string | null;
  /** Narrow the journal to one ticker; null shows every company. */
  filterTicker: string | null;
  onFilterChange: (ticker: string | null) => void;
  /** Tickers available to filter by. */
  tickers: string[];
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function money(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `$${n.toFixed(2)}`;
}

/**
 * Badge tone follows the PROCESS score, never the outcome — the same rule the
 * review panel itself applies. A trade that made money on bad reasoning must
 * not wear a green badge.
 */
function badgeTone(score: number | null | undefined): string {
  if (score === null || score === undefined) return "neutral";
  if (score >= 67) return "positive";
  if (score <= 33) return "negative";
  return "neutral";
}

export default function TradeHistory({
  trades,
  loading,
  error,
  filterTicker,
  onFilterChange,
  tickers,
}: TradeHistoryProps) {
  // Reviews keyed by trade, so a row can be badged without a request per row.
  const [reviews, setReviews] = useState<Record<number, StoredReview[]>>({});
  const [pendingCount, setPendingCount] = useState(0);
  const [onlyPending, setOnlyPending] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [reviewing, setReviewing] = useState<number | null>(null);
  const [rowError, setRowError] = useState<{ id: number; msg: string } | null>(null);

  const loadReviews = useCallback(async () => {
    try {
      const [all, pending] = await Promise.all([
        getReviews({ limit: 200 }),
        getPendingReviews(),
      ]);
      const byTrade: Record<number, StoredReview[]> = {};
      for (const r of all.reviews) {
        if (r.trade_id === null) continue;
        (byTrade[r.trade_id] ??= []).push(r);
      }
      setReviews(byTrade);
      setPendingCount(pending.count);
    } catch {
      // Badges are an enhancement; the journal itself must still render.
    }
  }, []);

  useEffect(() => {
    loadReviews();
  }, [loadReviews, trades.length]);

  async function handleReview(tradeId: number) {
    setReviewing(tradeId);
    setRowError(null);
    try {
      await reviewLoggedTrade(tradeId);
      await loadReviews();
      setExpanded(tradeId);
    } catch (err) {
      setRowError({
        id: tradeId,
        msg: err instanceof Error ? err.message : "The review failed.",
      });
    } finally {
      setReviewing(null);
    }
  }

  const visible = onlyPending
    ? trades.filter(
        (t) =>
          t.entry_rationale &&
          t.entry_rationale !== OPENING_RATIONALE &&
          !(reviews[t.id]?.length)
      )
    : trades;

  const pager = usePagination(
    visible,
    10,
    `${filterTicker ?? "all"}:${visible.length}:${onlyPending}`
  );

  return (
    <>
      {/* The backlog the user could not previously see: entries they wrote a
          reason for, submitted, and got nothing back on. */}
      {pendingCount > 0 && (
        <div className="journal-pending-banner">
          <span className="journal-pending-count">{pendingCount}</span>
          <span>
            logged {pendingCount === 1 ? "trade has" : "trades have"} never been
            reviewed.
          </span>
          <button
            className="btn-secondary-sm"
            onClick={() => setOnlyPending((v) => !v)}
          >
            {onlyPending ? "Show all trades" : "Show only these"}
          </button>
        </div>
      )}

      <div className="range-bar">
        <span className="range-bar-label">Company</span>
        <select
          className="trade-input trade-filter-select"
          value={filterTicker ?? ""}
          onChange={(e) => onFilterChange(e.target.value || null)}
        >
          <option value="">All companies</option>
          {tickers.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>

      {loading && <div className="journal-notice">Loading the journal…</div>}
      {error && <div className="journal-notice journal-error">{error}</div>}

      {!loading && !error && visible.length === 0 && (
        <div className="journal-notice">
          {onlyPending
            ? "Every logged trade has been reviewed."
            : "No trades logged yet. Your first entry — and the reasoning behind it — starts the history the coach will learn from."}
        </div>
      )}

      {!loading && !error && visible.length > 0 && (
        <>
          <div className="journal-list">
            {pager.pageItems.map((t) => {
              const isOpening = t.entry_rationale === OPENING_RATIONALE;
              const rowReviews = reviews[t.id] ?? [];
              const latest = rowReviews[0];
              const latestReport = latest?.report as CoachReport | undefined;
              const canReview = !isOpening && !!t.entry_rationale;
              const isOpen = expanded === t.id;

              return (
                <article key={t.id} className="journal-entry">
                  <div className="journal-entry-head">
                    <span
                      className={`journal-side journal-side-${t.side}`}
                      title={t.side === "buy" ? "Bought" : "Sold"}
                    >
                      {t.side === "buy" ? "BUY" : "SELL"}
                    </span>
                    <span className="journal-ticker">{t.ticker}</span>
                    <span className="journal-qty">{t.quantity} sh</span>
                    <span className="journal-price">
                      @ {money(t.execution_price)}
                    </span>
                    <span className="journal-total">{money(t.total_value)}</span>
                    <span className="journal-when">{formatWhen(t.executed_at)}</span>
                  </div>

                  {t.entry_rationale ? (
                    <p
                      className={`journal-rationale ${
                        isOpening ? "journal-rationale-seed" : ""
                      }`}
                    >
                      {isOpening && (
                        <span className="journal-seed-tag">seeded</span>
                      )}
                      {t.entry_rationale}
                    </p>
                  ) : (
                    <p className="journal-rationale journal-rationale-empty">
                      No rationale recorded — the coach has nothing to evaluate
                      for this trade.
                    </p>
                  )}

                  {t.avg_price_after !== null && (
                    <div className="journal-meta">
                      Average after this trade: {money(t.avg_price_after)}
                    </div>
                  )}

                  {canReview && (
                    <div className="journal-coach-row">
                      {latest ? (
                        <>
                          <span
                            className={`journal-review-badge tone-${badgeTone(
                              latestReport?.process_quality
                            )}`}
                            title="Quality of the reasoning, scored before the outcome was known"
                          >
                            Reviewed
                            {latestReport?.process_quality != null &&
                              ` · process ${latestReport.process_quality}/100`}
                          </span>
                          {latestReport?.luck_vs_skill && (
                            <span className="journal-quadrant-chip">
                              {latestReport.luck_vs_skill}
                            </span>
                          )}
                          <button
                            className="btn-secondary-sm"
                            onClick={() => setExpanded(isOpen ? null : t.id)}
                          >
                            {isOpen ? "Hide review" : `Read review${
                              rowReviews.length > 1 ? ` (${rowReviews.length})` : ""
                            }`}
                          </button>
                          <button
                            className="btn-coach btn-coach-sm"
                            disabled={reviewing === t.id}
                            onClick={() => handleReview(t.id)}
                            title="Review again — a later verdict can differ once more time has passed"
                          >
                            {reviewing === t.id ? "Reviewing…" : "Review again"}
                          </button>
                        </>
                      ) : (
                        <>
                          <span className="journal-review-badge journal-review-none">
                            Not yet reviewed
                          </span>
                          <button
                            className="btn-coach btn-coach-sm"
                            disabled={reviewing === t.id}
                            onClick={() => handleReview(t.id)}
                          >
                            {reviewing === t.id
                              ? "Reviewing…"
                              : "🧠 Review this trade"}
                          </button>
                        </>
                      )}
                    </div>
                  )}

                  {rowError?.id === t.id && (
                    <div className="trade-error">{rowError.msg}</div>
                  )}

                  {/* Every review of this trade, newest first. More than one is
                      normal: the same decision judged after 7 days and after 90
                      can reasonably differ, and where they differ is the point. */}
                  {isOpen &&
                    rowReviews.map((r) => (
                      <div key={r.id} className="journal-review-wrap">
                        <div className="journal-review-stamp">
                          Reviewed {formatWhen(r.created_at)}
                          {r.model && ` · ${r.model}`}
                        </div>
                        <CoachReview report={r.report as CoachReport} />
                      </div>
                    ))}
                </article>
              );
            })}
          </div>

          <Pagination
            page={pager.page}
            pageCount={pager.pageCount}
            onChange={pager.setPage}
            rangeStart={pager.rangeStart}
            rangeEnd={pager.rangeEnd}
            total={pager.total}
          />
        </>
      )}
    </>
  );
}
