# Phase 8: Persistent Reviews and Retrospective Coaching on a Single Log

**Goal**: Make coaching available on a trade the user has **already logged**, and
stop throwing every review away the moment it is rendered.

**Depends on**: nothing in phases 1-7. This phase is **independent of the cash
ledger** and can be built first if the coaching gap matters more than net worth.
It composes with phase 7 (position sizing) when both exist.

## The gap

`POST /coach/review` is pre-trade only — its own schema says so
(`api_schemas.py:663`: *"a PRE-trade review"*). It takes an `entry_rationale`
string and no `trade_id`. `CoachReview` is rendered inside `TradeForm` and
cleared on a successful log (`TradeForm.tsx`).

So the moment the user presses **Log trade**, that entry becomes permanently
un-coachable. Any rationale written without first pressing *Get coach review* —
which is every trade logged in a hurry, i.e. exactly the ones worth reviewing —
receives no feedback ever.

Reviews are also never stored: `grep -rn "coach_review" backend/` returns
nothing. The coach therefore cannot know it has said something before, and the
user cannot see what they were told last time.

## The design problem: hindsight

A retrospective review knows what happened next. That is the whole value, and
also the whole danger. A coach that says *"you were wrong, the price fell"*
teaches outcome-chasing — the single most destructive habit in trading, and one
this system is otherwise built to fight.

So the review must separate two independent judgements:

| | Good outcome | Bad outcome |
|---|---|---|
| **Good process** | Repeat it | **Bad luck** — change nothing |
| **Bad process** | **Most dangerous** — a bad habit just got rewarded | Fix it |

The two off-diagonal cells are the ones a naive review gets wrong, and they are
where the coaching value is.

### Enforce it structurally, not just in the prompt

Generate the review in **two passes**:

1. **Pass 1 — process.** The model sees only the rationale and the data that
   existed **at or before** `executed_at`. It never sees what happened next. It
   produces `process_quality`, `what_was_knowable`, and the bias findings.
2. **Pass 2 — outcome.** The model is given pass 1's verdict *as fixed input*
   plus the realized outcome, and produces `outcome_summary` and
   `luck_vs_skill`. It is explicitly told it may not revise pass 1.

Asking a single call to "judge the process without being influenced by the
outcome" does not work — the outcome is in its context and it will rationalize
backwards. Withholding the outcome from pass 1 makes the separation real. This is
the same posture as `verify_citations`: enforce the property rather than
requesting it.

## Tasks

1. **`coach_reviews` table** — `services/db.py`

   ```sql
   CREATE TABLE IF NOT EXISTS coach_reviews (
       id            INTEGER PRIMARY KEY AUTOINCREMENT,
       review_type   TEXT    NOT NULL CHECK (review_type IN
                       ('pre_trade','retrospective','journal')),
       trade_id      INTEGER,
       ticker        TEXT,
       scope         TEXT,
       rationale_snapshot TEXT,
       report_json   TEXT    NOT NULL,
       model         TEXT,
       data_as_of    TEXT,
       created_at    TEXT    NOT NULL,
       FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE CASCADE
   );
   CREATE INDEX IF NOT EXISTS idx_reviews_trade ON coach_reviews (trade_id);
   CREATE INDEX IF NOT EXISTS idx_reviews_type_time
       ON coach_reviews (review_type, created_at);
   ```

   - `rationale_snapshot` copies the rationale as it read **at review time**. If
     the user later edits the entry, the review must still show what was actually
     judged, or the record becomes misleading.
   - `report_json` stores the whole `CoachReport`, so a schema change does not
     invalidate old reviews. Read it back through the Pydantic model with
     tolerant defaults.
   - `data_as_of` records which analysis run backed the review (task 3), so a
     later reader can tell what the coach could see.
   - `model` records the LLM used. `_DEFAULT_MODEL` has already changed once this
     project (`gemini-2.5-flash` → `gemini-3.6-flash`); reviews written by
     different models should be distinguishable.

2. **`services/review_store.py`** — repository, same posture as `cash_service`

   ```python
   def save_review(report, review_type, trade_id=None, ticker=None,
                   scope=None, rationale_snapshot=None, data_as_of=None) -> dict
   def get_review(review_id) -> dict | None
   def reviews_for_trade(trade_id) -> list[dict]        # newest first
   def list_reviews(review_type=None, ticker=None, limit=50) -> list[dict]
   def unreviewed_trades(limit=None) -> list[dict]      # the user's actual complaint
   def latest_review_per_trade() -> dict[int, dict]
   ```

   **Persist every review, including the existing pre-trade one.** A pre-trade
   review that was given and then ignored is one of the most informative records
   the journal can hold, and phase 9 depends on having it.

   `unreviewed_trades` is the direct answer to "I logged it and got no feedback":
   trades with an `entry_rationale` and no row in `coach_reviews`.

3. **Point-in-time data — use `rag/history_store.py`**

   For a retrospective review, handing the coach today's SEC and technical
   reports is hindsight contamination: the current technical report already knows
   the price fell.

   `history_store` persists every analysis run as
   `analysis_history/{TICKER}_{run_id}.json` and exposes
   `get_analysis_history(ticker, limit)` and `get_analysis(run_id)`. Add:

   ```python
   def analysis_as_of(ticker: str, when: datetime) -> dict | None:
       """The most recent stored analysis run at or before `when`. None if the
       trade predates any run for this ticker."""
   ```

   Resolution order for pass 1's data, and it must be recorded in `data_as_of`:

   1. A stored run at or before `executed_at` — the honest case.
   2. No such run → hand the coach **no** fundamental/technical pillar and add an
      explicit `data_limitations` entry saying the review rests on the rationale
      and the journal alone. Do **not** substitute the current report.

   Price data is different and safe: `price_provider` can fetch the true
   as-of-date window, so pass 1 may see prices up to `executed_at` and no later.

4. **Extend `CoachReport`** — `agents/schemas/coach.py`

   ```python
   review_type: str = "pre_trade"
   trade_id: int | None = None

   # retrospective only; None on a pre-trade review
   process_quality:   int | None      # 0-100, judged WITHOUT the outcome
   what_was_knowable: str | None      # what the data said at executed_at
   outcome_summary:   str | None      # what actually happened, 7/30/90d
   luck_vs_skill:     str | None      # one of the four quadrants, named
   hindsight_note:    str | None      # why process and outcome are scored apart
   ```

   `alignment_score` keeps its current meaning for pre-trade reviews.
   `process_quality` is the retrospective analogue and is deliberately a separate
   field: conflating them would let a retrospective score silently absorb outcome
   information.

5. **`CoachAgent.analyze_retrospective(trade_id)`** — `agents/coach_agent.py`

   Implements the two passes above. Reuse without modification:
   `journal_analysis.trade_outcomes` (already joins each trade to its 7/30/90-day
   **signed** result — a sell is correct when the price falls), the
   `MIN_TRADES_FOR_PATTERN = 5` cold-start gate, and `verify_citations`.

   Two rules carried over intact:
   - `history_sufficient` gating still forces `historical_pattern = None`.
   - A horizon that has not elapsed already returns
     `{"price": None, "return": None, "note": "horizon has not elapsed yet"}` —
     so a trade logged yesterday yields a **process-only** review, with
     `outcome_summary` stating that it is too early to say. Do not let the model
     fill that silence.

6. **Router** — `backend/routers/coach.py`

   ```
   POST /coach/review                      unchanged; now also persists
   POST /coach/review/trade/{trade_id}     retrospective review of one log
   GET  /coach/reviews                     list, filterable by type/ticker
   GET  /coach/reviews/trade/{trade_id}    every review of this trade, newest first
   GET  /coach/reviews/pending             trades with a rationale and no review
   ```

   Keep the existing 503-when-no-API-key guard and the `data_limitations` note
   appended when neither pillar is available (`routers/coach.py:82`) — the
   retrospective path needs it more often, not less.

   `POST .../trade/{trade_id}` on an already-reviewed trade creates a **new**
   review rather than replacing the old one. A trade reviewed at 7 days and again
   at 90 days is two legitimately different judgements, and the history is the
   point.

## Definition of done
- A trade logged **without** pressing *Get coach review* can be reviewed
  afterwards through `POST /coach/review/trade/{id}` and gets a full report.
- `GET /coach/reviews/pending` lists exactly the trades that have a rationale and
  no review.
- Every review — pre-trade included — appears in `coach_reviews` afterwards.
- Reviewing a trade whose thesis was sound but whose price fell produces a
  **high `process_quality` with a negative outcome**, and `luck_vs_skill` names
  the bad-luck quadrant. This is the assertion the phase exists for.
- The inverse case — a rationale full of panic language that happened to work —
  produces a **low `process_quality` with a positive outcome** and an explicit
  warning not to repeat it.
- Pass 1's prompt provably contains no post-`executed_at` information: verify by
  inspecting the captured `raw_data`, which `analyze` already records via
  `capture["raw_data"]`.
- A trade predating any stored analysis run gets no fundamental pillar and says
  so, rather than being handed today's report.
- A trade logged yesterday returns a process review and declines to state an
  outcome.
- `data_as_of` on the saved row names the analysis run used, or is null with a
  recorded reason.
