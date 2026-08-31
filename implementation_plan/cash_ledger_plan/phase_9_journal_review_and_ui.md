# Phase 9: Journal-Wide Review, Review History, and the UI

**Goal**: Let the user ask the coach about their **whole record at once**, see
every past review beside the trade it judged, and reach coaching from anywhere in
the journal — not only from the trade form.

**Depends on**: Phase 8. Composes with phase 7 when both exist.

## Tasks

1. **`CoachAgent.analyze_journal(scope)`** — the whole-record review

   ```python
   scope = {"ticker": str | None,      # None = the entire portfolio
            "since": str | None,       # ISO date, None = everything
            "limit": int}
   ```

   Not a loop over single-trade reviews. It answers questions that only exist at
   the level of the whole record and that no per-trade review can reach:

   - **Which patterns actually recur**, with the dates that evidence them.
   - **Does process quality correlate with outcome?** Across reviewed trades, do
     high-`process_quality` decisions actually do better? If they do not, either
     the process scoring or the strategy is wrong, and that is worth saying.
   - **Do emotional and analytical rationales perform differently**, using
     `classify_rationale`'s existing labels — carrying over the standing caveat
     that it is a keyword match and a weak hint, not a psychological assessment.
   - **Which advice was given and then ignored.** Phase 8 persists pre-trade
     reviews, so the coach can compare what it warned about against what the user
     then logged. This is the most valuable output of the phase and it is
     impossible without review persistence.
   - **What changed over time** — is the same bias fading or hardening?

   Cold start: the existing `MIN_TRADES_FOR_PATTERN = 5` gate governs here too.
   Below it, return a review that says the record is too short and makes no
   claims about tendencies. Reuse the gate; do not invent a second threshold.

2. **`JournalReport` schema** — `agents/schemas/coach.py`

   ```python
   class RecurringPattern(BaseModel):
       pattern: str
       occurrences: list[str]          # dates — verified against the real journal
       trend: str                      # "worsening" | "stable" | "improving"
       evidence: str

   class JournalReport(AgentReport):
       agent: str = "trading_coach"
       review_type: str = "journal"
       scope_description: str          # exactly what was reviewed, in words
       trades_reviewed: int
       period: str | None
       recurring_patterns: list[RecurringPattern]
       process_vs_outcome: str         # does good process actually pay here?
       advice_followed: str | None     # what was warned about, and what happened
       strengths: list[str]            # a coach names what works, not only faults
       priorities: list[str]           # at most 3, ordered
       history_sufficient: bool
       data_limitations: list[str]
   ```

   `strengths` is required, not decorative. A review that only lists faults is
   read once and then avoided, and the user stops asking — which costs more than
   any single missed correction.

   `priorities` is capped at three. A list of twelve things to fix is a list of
   zero things that will be fixed.

3. **Extend `verify_citations` to the journal report**

   `verify_citations` (`coach_agent.py:140`) currently walks
   `report.detected_biases[].past_occurrences`. `RecurringPattern.occurrences` is
   a second place dates appear, and an unverified date there is exactly the
   failure the function exists to prevent. Make it walk both, and cover phase 7's
   cash-flow dates in the same pass.

   Structure the function so a **new list of dates cannot be added without being
   verified** — take the date lists as a collected set of references rather than
   naming each field inline. Each new report field is otherwise a new place to
   fabricate.

4. **Router** — `backend/routers/coach.py`

   ```
   POST /coach/review/journal          whole-record review; body carries scope
   ```

   Persists as `review_type='journal'` with `scope` recorded, so successive
   journal reviews are comparable over time.

5. **UI — the journal becomes the entry point**

   `components/portfolio/TradeHistory.tsx` already renders the full untruncated
   rationale, tags seeded `OPENING_RATIONALE` entries, and paginates via
   `usePagination`. Add per row:

   - a **review badge** — reviewed (with its `process_quality` and a hint of the
     luck/skill quadrant) or **not yet reviewed**;
   - a **🧠 Review this trade** button on any unreviewed row with a rationale;
   - expanding a reviewed row shows the stored report inline, using the existing
     `CoachReview` component, plus every earlier review of the same trade in date
     order — a 7-day and a 90-day verdict are both legitimate and their divergence
     is informative.

6. **UI — the unreviewed backlog**

   A banner above the journal: *"12 logged trades have never been reviewed"*,
   from `GET /coach/reviews/pending`, with a filter to show only those. This is
   the user's original complaint stated as a workflow: the entries they submitted
   and got nothing back on must be findable in one click.

   Offer reviewing them one at a time. **Do not offer a bulk "review all" button**
   — it would fire N LLM calls on one click, and phase 5 of `trading_coach_plan`
   already established the precedent of not spending a multi-agent budget without
   the user asking (`portfolio_service.py:461`, the deferred baseline debate).

7. **UI — journal review panel**

   `components/portfolio/JournalReview.tsx`, reached from a **"🧠 Review my whole
   journal"** button in the Trading Journal header and scoped by the ticker filter
   already in that view. Renders recurring patterns with their verified date
   chips (reuse `.coach-date-chip`), strengths, the at-most-three priorities, and
   the `advice_followed` narrative.

   Keep the `coach-caution` banner when `history_sufficient` is false — a
   confident-sounding review of six trades is precisely what teaches misplaced
   trust.

8. **UI — review history view.** A list of every past review, filterable by type
   and ticker, so the user can read what they were told three months ago. Reuse
   `usePagination` and the section-14/15 styles; add section 17 to `index.css`.

9. **Coach chat gets the review history.** `_coach_chat_persona`
   (`routers/chat.py`) already works with no ticker and no prior analysis. Add
   recent reviews and the pending count so the user can ask *"what have you been
   telling me?"* and *"what did I ignore?"* in conversation.

## Definition of done
- **The complaint is closed**: a trade logged without a review can be reviewed
  from the journal row, and the unreviewed backlog is visible and clickable.
- `POST /coach/review/journal` on a seeded journal returns recurring patterns
  whose every cited date exists in the real journal — verified through HTTP, the
  way phase 6 of `trading_coach_plan` was.
- A fabricated date placed in `RecurringPattern.occurrences` is stripped by
  `verify_citations`, exactly as one in `detected_biases` already is.
- Scoping the review to one ticker reviews only that ticker's trades, and
  `scope_description` says so in words.
- With fewer than `MIN_TRADES_FOR_PATTERN` trades, the journal review claims no
  tendencies and shows the caution banner.
- `priorities` never exceeds three entries; `strengths` is non-empty whenever any
  trade scored well.
- A trade carrying two reviews shows both, in date order, without one overwriting
  the other.
- `advice_followed` correctly identifies a case where a pre-trade review warned
  about something and the user logged the trade anyway.
- No bulk-review control exists anywhere in the UI.
- `npx tsc --noEmit` and `npx vite build` clean.
