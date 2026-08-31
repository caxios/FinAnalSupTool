# Phase 7: The Agents Learn About Size and Currency

**Goal**: Let the Coach and Quant Risk agents reason about *how much* the user is
committing and *in which currency*. A trade is a different decision at 2% of net
worth than at 40%, and a US purchase is two decisions — the stock and the dollar —
that the app has never been able to separate.

**Depends on**: Phases 5, 6.

## Tasks

1. **A fourth, computed pillar in the Coach's context** — `agents/coach_agent.py`

   The agent assembles three pillars (fundamental, technical, behavioural) and
   receives `proposed_side` / `proposed_quantity` with no way to judge their
   scale. Add:

   ```python
   {
     "net_worth_krw", "net_worth_usd", "cash_balances", "cash_weight",
     "position_current_weight",       # of net worth, before this trade
     "position_weight_after",
     "trade_size_pct_of_net_worth",
     "trade_size_pct_of_cash",        # 1.0 = deploying every remaining unit
     "trade_currency",                # the asset's denomination
     "requires_conversion",           # true when that currency's cash won't cover it
     "fx_exposure_before", "fx_exposure_after",
     "dry_powder_days",
     "largest_position_weight",
   }
   ```

   Keep the module's established division of labour: **Python computes, the LLM
   interprets.** These are numbers, not judgements, and they enter the prompt as
   structure — the same discipline that makes `verify_citations` possible.

2. **Sizing and currency rules in `_SYSTEM_PROMPT`**

   Four observations the coach could not previously make, each requiring evidence:

   - **Concentration**: "this buy takes AAPL from 22% to 38% of your net worth" —
     a factual restatement, and often the whole point.
   - **Dry powder exhaustion**: "this deploys 94% of your remaining dollars; you
     will have no capacity to average down if the thesis takes longer than you
     expect." Especially pointed when the rationale is urgency-flavoured.
   - **The hidden second decision**: a US purchase funded by conversion is a bet
     on the stock *and* on the dollar. "This raises your USD exposure from 51% to
     64% of net worth. Your rationale addresses the company; it does not mention
     the currency." Buying US equity after the won has already weakened sharply is
     worth naming as a fact about entry rate, not as a forecast.
   - **Sizing versus conviction**: flag a hedged, uncertain rationale paired with
     an unusually large position, or a strongly argued one with a token position.
     The gap between what someone says and what they stake is the most legible
     behavioural signal the journal now contains.

   **Do not let the coach forecast exchange rates.** It may state exposure,
   entry rate versus history, and what the rationale omitted. It may not say
   where USDKRW is going. Add this to the ABSOLUTE RULES block beside the
   existing prohibition on inventing trades — the failure mode is the same
   (confident claims the data does not support) and it belongs in the same place.

3. **Sizing and currency history in `services/journal_analysis.py`**

   `pattern_summary()` gains statistics, all under the existing
   `MIN_TRADES_FOR_PATTERN = 5` cold-start gate — no new gating logic:

   ```python
   "avg_trade_size_pct", "largest_trade_size_pct",
   "emotional_vs_analytical_sizing",   # mean size by rationale class
   "cash_deployment_pattern",          # do buys cluster at low cash?
   "outcome_by_size_quartile",         # do the big ones actually work out?
   "conversion_timing",                # entry rates vs. the period's range
   "outcome_local_vs_base",            # how often FX flipped the result's sign
   ```

   `outcome_by_size_quartile` is the most useful thing here: it can tell the user,
   from their own record, whether their high-conviction sizing has historically
   been rewarded. `outcome_local_vs_base` is the second — a user whose stock picks
   work but whose won-denominated results do not has a currency problem, not a
   selection problem, and no existing view can tell them that.

   Reuse `trade_outcomes`, which already joins each trade to its 7/30/90-day
   signed result; extend it to carry both the local and base-currency return.

   The honesty rules carry over unchanged: `classify_rationale` is a keyword
   match and the prompt already says so. Sizing statistics must not acquire more
   authority than the classifier beneath them deserves.

4. **New bias types, each now evidenceable**

   Add to the prompt's bias list:
   - **over-concentration** — repeatedly sizing into an already-largest position
   - **cash-drag anxiety** — deploying immediately after every deposit
   - **panic de-risking** — a large withdrawal or sell cluster after a drawdown
   - **currency chasing** — converting to USD in bulk right after a sharp won
     move, which the ledger's conversion history can now show

   `verify_citations` (`coach_agent.py:140`) strips any cited date absent from the
   real journal. **Extend it to cover cash flows and conversions**, so a claim
   about a deposit or a 환전 is held to exactly the same standard as one about a
   trade. This is the phase's main regression risk: a new data source the verifier
   does not know about is a new place to fabricate.

5. **Coach chat persona** — `routers/chat.py`

   `_coach_chat_persona` already works with no ticker and no prior analysis. Add
   the cash, sizing and FX-exposure summary so the coach can answer "how much
   cash do I have?", "what is my biggest position?" and "how exposed am I to the
   dollar?" directly.

6. **Quant Risk agent** — already receives `cash` and `fx_returns` from phase 5.
   Surface the `fx_risk` block in its report and in its chat persona, including
   the counter-intuitive result when `fx_contribution` is negative: for a Korean
   investor, dollar exposure can *reduce* total portfolio volatility, and the
   agent should be able to explain that rather than treating every exposure as
   risk added.

## Definition of done
- A trade worth 40% of net worth draws an explicit sizing observation naming the
  before/after weight; a 2% trade draws none.
- Deploying the last of a currency's cash is called out with the actual percentage.
- A US purchase requiring conversion draws a currency observation naming the
  before/after FX exposure; a Korean purchase of the same size does not.
- The coach never states a directional view on USDKRW — verify with a rationale
  that explicitly invites one.
- With fewer than `MIN_TRADES_FOR_PATTERN` trades, no sizing, conversion or
  currency pattern is claimed; the existing cold-start gate covers the new
  statistics.
- A fabricated cash-flow or conversion date is stripped by `verify_citations`,
  exactly as a fabricated trade date already is.
- `outcome_by_size_quartile` and `outcome_local_vs_base` on a seeded journal match
  hand calculations.
- The coach chat answers a cash and an FX-exposure question with no ticker and no
  prior analysis.
