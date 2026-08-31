# Phase 6: Multi-Currency UI — Cash, Allocation, Attribution

**Goal**: Show net worth, allocation and return in **both KRW and USD**, make
each holding's own denomination unambiguous, and give the user somewhere to
record deposits, withdrawals and 환전.

**Depends on**: Phase 4.

## Tasks

1. **One `<Money>` component, used everywhere** — `components/portfolio/Money.tsx`

   ```tsx
   <Money krw={h.market_value_krw} usd={h.market_value_usd} />
   // ₩16,450,000
   // $12,340.00
   ```

   `views/Portfolio.tsx` currently formats money through a local `money()` helper
   (`Portfolio.tsx:33`) that hardcodes `currency: "USD"`. Replace it here rather
   than adding a second formatter — two of them will diverge.

   **Both currencies, always** (README decision 0). This holds for a Korean
   holding and a US holding alike, for every subtotal, and for net worth. A
   Samsung position shows a dollar figure; an Apple position shows a won figure.

   - Won on the primary line, dollars beneath, dimmed. `ko-KR` / `en-US` locales,
     and **no decimal places for won** — ₩16,450,000.00 is noise.
   - The component takes two already-computed numbers and **never multiplies by a
     rate**. Conversion happens once, on the backend (phase 4), so two places in
     the UI can never disagree about what a figure is worth.
   - Either value `null` (FX unavailable) renders that line as a dash, never a
     zero, and never a stale figure.
   - A `compact` prop for dense table cells: both values inline on one line,
     separated by a thin divider, rather than dropping one. Dropping a currency
     in the table would defeat the requirement precisely where the comparison
     between a Korean and a US holding is being made.
   - An optional KRW-only / USD-only / Both toggle in the view header, persisted
     to `localStorage`, defaulting to **Both**. It is a convenience for a user who
     wants to scan one column, not the default behaviour.

2. **Currency badge on every holding row.** A small `USD` / `KRW` chip beside the
   ticker marking what the asset actually *trades* in — distinct from the two
   figures shown, which are the same wealth stated twice. Without it,
   `005930.KS` and `AAPL` give no hint which of their two numbers is the traded
   price and which is a conversion.

3. **Net worth header** — a summary strip above Holdings

   Net worth in both currencies; the split into Equity and Cash with weights; a
   slim allocation bar. Beneath it the `fx` rate with its `as_of` and a **stale**
   badge when `is_stale` — a rate the user cannot date is a rate they cannot
   check.

   Add **FX exposure** here as its own figure ("57% of your net worth is
   dollar-denominated"). It is the number that summarises this user's whole
   currency situation and it exists nowhere in the app today.

4. **Cash cards — one per currency.** Balance, weight, `dry_powder_days`, and
   Deposit / Withdraw buttons, plus a **Convert (환전)** action between them.

   When `is_initialized` is false, this area is instead a short setup form
   ("How much cash do you hold, in each currency?") calling
   `POST /portfolio/cash/initialize`. Say in one line that it is an anchor rather
   than reconstructed history — matching how `TradeHistory.tsx` already labels
   seeded `OPENING_RATIONALE` entries.

5. **`weight` column, and cash rows inside the holdings table.** Cash belongs in
   the allocation table, not beside it — it is a position, and separating it
   invites reading the equity weights as if they were the whole.

6. **Cash flow form** — `components/portfolio/CashFlowForm.tsx`

   Type, amount, **currency selector**, date. Follow `TradeForm`'s conventions
   exactly: `nowLocalInput()` / `toUtcIso()` for the timestamp — the
   `datetime-local` → UTC conversion is already solved there and must not be
   re-solved — and a collapsed manual override for the rate, showing the
   auto-resolved value as its placeholder.

7. **Conversion form** — `components/portfolio/ConvertForm.tsx`

   From-currency + amount, to-currency + amount, date. **Take both amounts from
   the user and derive the rate**, rather than asking for a rate: that is what
   their bank statement shows, and it captures the spread they actually paid.
   Display the derived rate against the market rate for that day, and show the
   spread as a won figure — it is a cost, and it should be visible as one.

8. **Return attribution panel** — the payoff for the whole plan

   Three bars or a small waterfall: **stock return / currency return / total**,
   for the portfolio and per position. One line of explanation, e.g. *"Your
   holdings gained 8.2%; the won weakened 3.1% against the dollar, adding 3.1%.
   Total in won: +11.6%."*

   Render the cross term correctly — 8.2% and 3.1% compose to 11.55%, not 11.3%.
   Do not compute the total by addition in the frontend; use `roi_base` from the
   backend, which is the figure the phase 4 identity is asserted against.

9. **Performance panel** — TWR and MWR side by side with one line on the
   difference, the net-worth chart from `net_worth_series`, and realized P/L,
   realized FX P/L, fees, taxes and conversion spread to date. Grey out the region
   before `coverage_start` so the chart does not imply history the ledger lacks.

10. **Warnings surfaced, not swallowed**
    - `cash_warning` from a trade (phase 3) → an inline caution on the confirmation.
    - A negative balance → a reconciliation prompt offering an `adjustment` flow.
    - A Korean holding → a quiet note that fundamental analysis is unavailable for
      non-US listings (README scope boundary), so its absence never reads as a
      clean bill of health.

11. **Ledger view** — `components/portfolio/CashLedger.tsx`

    A tab beside the Trading Journal: every flow, newest first, with a running
    per-currency balance, type and currency filters, and the `usePagination` +
    `Pagination` pattern `TradeHistory` already uses. The two legs of a conversion
    render as **one row**, joined by `conversion_id`. Trade-linked flows show
    their ticker and link to the journal entry; synthetic setup rows are tagged
    the way seeded opening trades already are, so an anchor is never mistaken for
    a real event.

12. **`index.css` section 16 — Cash, Currency & Allocation.** Follow sections 14
    and 15; reuse the existing `tone-*`, `.fin-table` and `.portfolio-*` classes
    rather than introducing a parallel set.

## Definition of done
- **Every money figure on the page renders in both won and dollars** — each
  holding, each cash balance, each subtotal, and net worth. Verified on a
  portfolio holding a Korean stock, a US stock, won cash and dollar cash at once.
- Net worth in won equals the sum of all four components converted to won, and
  net worth in dollars equals the same four converted to dollars; the two totals
  reconcile to each other at the displayed rate.
- The frontend performs **no** currency multiplication — both figures come from
  the API.
- The KRW/USD/Both toggle defaults to Both, switches all figures at once, and
  survives a reload.
- Weights in the table plus the cash rows sum to a visible 100%.
- With FX unavailable, each holding still shows its own traded-currency figure,
  the converted counterpart shows `—`, and no zeros or stale values appear.
- A fresh install shows the setup form; afterwards each balance equals what was
  entered.
- Logging a deposit or a conversion updates net worth, weights and FX exposure
  with no manual refresh.
- The attribution panel's three figures reconcile multiplicatively, not additively.
- `npx tsc --noEmit` and `npx vite build` both clean.
