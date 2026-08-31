# Phase 6: Adaptive Trading Coach Agent

**Goal**: Implement blueprint §3 — the meta-cognitive coach that evaluates the
user's stated **Entry Rationale** against objective AI data and names the
psychological bias, citing the user's own trade history.

**Depends on**: Phases 2 (journal + rationales) and 5 (risk metrics).

**Note**: all three data pillars already exist in the codebase —
`SECFilingsAgent` (fundamental), `TechnicalAnalysisAgent` + `price_provider`
(technical), and the phase-2 journal (behavioral). This phase is synthesis, not
new data acquisition.

## Tasks:

1. **Create `backend/services/journal_analysis.py`** (deterministic
   pre-processing, so the LLM reasons over structure rather than raw text)
   - `trade_outcomes(ticker=None) -> list[dict]`: join each trade to the price N
     days later (7 / 30 / 90) via `price_provider`, yielding what actually
     happened after each decision. This is what makes a claim like "in your last 3
     similar trades…" evidence-backed instead of fabricated.
   - `rationale_corpus(ticker=None) -> list[dict]`: `(executed_at, side,
     rationale, outcome)` tuples, newest first.
   - `pattern_summary() -> dict`: counts by side, average holding period, win
     rate, and — the behavioral signal — win rate on trades whose rationale text
     matches emotional markers versus analytical ones. Keep the keyword list in
     one named constant so it stays auditable and editable.
   - **Handle the cold-start case explicitly**: with fewer than ~5 trades there is
     no pattern. The coach must report "not enough history yet" rather than
     generalizing from two data points. An invented pattern is worse than silence
     here, because the user may trade on it.

2. **Create `backend/agents/coach_agent.py`**
   - Subclass `BaseAgent`; `agent_id = "trading_coach"`.
   - Schema `backend/agents/schemas/coach.py`: `CoachReport` with
     `rationale_evaluation` (the user's stated logic against objective data),
     `detected_biases` (list of `{bias, evidence, past_occurrences}`),
     `historical_pattern`, `coaching_feedback`, and `alignment_score` (0-100),
     plus the base `confidence` / `reasoning` fields.
   - Context assembled from the three pillars: the SEC agent's report, the
     Technical agent's report, and `journal_analysis` output.
   - System prompt requirements:
     - Cite **specific past trades by date** — never a generic bias lecture.
     - Name the conflict explicitly when rationale and data disagree (the
       blueprint's worked example: selling on broken technicals while
       fundamentals are up 20%, where the last 3 such trades missed a rebound).
     - **Never fabricate a past trade.** Every historical claim must trace to a
       row returned by `trade_outcomes`; if the history is empty, say so.
     - Be direct but not moralizing — this is a coach, not a scold.

3. **Expose the coach**
   - `POST /coach/review` — body `{ ticker, proposed_side, proposed_quantity,
     entry_rationale }` returns a pre-trade review the user can request *before*
     committing, which is where coaching actually changes behavior.
   - Optionally also run it as a post-trade reflection on `POST /portfolio/trades`.
   - Register in `agentMeta.ts` so it renders like the other agents.
   - Add it as a `ChatPanel` persona: the persona path in `routers/chat.py` reads
     `debate_store.get(ticker)`, so either wire the coach's report into that
     record or give it a dedicated branch, so "chat with your coach" works.

4. **UI surface**
   - Render the coach's review inside the phase-4 `TradeForm`: once the user has
     written a rationale, a "Get coach review" action returns the evaluation
     *before* the trade is logged.
   - Show `alignment_score` and the detected biases using the existing tone
     classes.

## Definition of done
- A trade whose rationale contradicts the fundamentals produces a specific,
  evidence-cited warning.
- With an empty journal the coach reports insufficient history and invents nothing.
- Every historical claim in the output corresponds to a real logged trade.
