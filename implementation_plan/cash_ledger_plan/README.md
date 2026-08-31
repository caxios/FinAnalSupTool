# Cash, Multi-Currency & Net Worth — Phased Implementation Plan

Extends the portfolio built in `trading_coach_plan/` (phases 1-6, implemented)
with the two things it does not model: **cash**, and the fact that the user holds
assets denominated in **more than one currency**.

## The user's actual situation

Korean-resident investor who holds **Korean stocks in KRW**, converts KRW to USD
to buy **US stocks**, and therefore carries **exchange-rate exposure as a real
component of return and risk** — not as a display preference.

Everything below follows from that. An earlier draft of this plan assumed a
KRW-based investor holding only USD assets and made USD the accounting currency;
that assumption does not survive Korean holdings and has been reversed.

## Phase order & dependencies

| Phase | File | Scope | Depends on |
|---|---|---|---|
| 1 | `phase_1_cash_ledger.md` | `cash_flows` schema, per-currency balances, opening anchor | — |
| 2 | `phase_2_fx_and_currency.md` | Asset currency, USDKRW provider, base-currency price series | 1 |
| 3 | `phase_3_trades_and_conversions.md` | Trades move cash; 환전; realized P/L and realized FX | 1, 2 |
| 4 | `phase_4_net_worth_returns.md` | Net worth, weights, three-way return attribution, TWR/MWR | 3 |
| 5 | `phase_5_risk_in_base_currency.md` | FX as a risk factor — corrects both over- and under-stated risk | 4 |
| 6 | `phase_6_multi_currency_ui.md` | Dual-currency display, cash cards, attribution, ledger view | 4 |
| 7 | `phase_7_agent_integration.md` | Coach position sizing, FX exposure, dry powder | 5, 6 |
| 8 | `phase_8_persistent_retrospective_review.md` | Persist reviews; coach a trade **already logged** | — |
| 9 | `phase_9_journal_review_and_ui.md` | Whole-journal review, review history, journal UI | 8 |

**Phases 8-9 depend on nothing in phases 1-7** and can be built first. They close
a separate gap: `POST /coach/review` is pre-trade only (`api_schemas.py:663`
says so outright), so the moment a user presses *Log trade* that entry becomes
permanently un-coachable — and no review is stored anywhere, so the coach cannot
remember what it has already said. If the coaching gap matters more than net
worth, start at phase 8.

## The four decisions everything follows from

### 0. Every amount is displayed in BOTH KRW and USD, always

This is a hard product requirement, not a preference. Korean stocks, US stocks,
won cash and dollar cash all roll up into **one net worth, which is then shown
twice** — fully converted to won, and fully converted to dollars. The same
applies to every subtotal and every individual position.

```
Net worth        ₩142,300,000   /   $105,407
  Equity         ₩118,500,000   /   $ 87,778
    005930.KS    ₩ 42,000,000   /   $ 31,111
    AAPL         ₩ 76,500,000   /   $ 56,667
  Cash           ₩ 23,800,000   /   $ 17,630
    KRW balance  ₩ 12,000,000   /   $  8,889
    USD balance  ₩ 11,800,000   /   $  8,741
```

Every monetary field in the API therefore ships as a `_krw` **and** a `_usd`
sibling. There is no "base-currency figure with the native one underneath" — a
Korean holding must still show a dollar figure, and a US holding a won figure.

**Naming trap to avoid**: "cash in KRW" is ambiguous between *the won balance*
and *all cash expressed in won*. Use `cash_balances: {"KRW": …, "USD": …}` for
the actual per-currency balances, and `cash_total_krw` / `cash_total_usd` for the
whole cash pile expressed in each. Same pattern for equity and net worth.

### 1. The base currency for *computation* is KRW

Displaying both currencies does not remove the need to pick one for the maths.
A percentage is currency-dependent in a way an amount is not: a position can be
**+5% in dollars and −2% in won** at the same time, and both are correct. So
every ratio — return, volatility, VaR, weight — must carry a currency label, and
the risk model has to be computed in one currency to be internally consistent.

That currency is the won, because risk and return are only meaningful relative to
what the investor actually consumes in. Make it a setting (`BASE_CURRENCY`,
default `KRW`) rather than a constant; the code paths are identical either way.

Percentages are reported in both currencies wherever the difference is
meaningful (phase 4's attribution, TWR/MWR), each explicitly labelled. Amounts
are *always* reported in both.

### 2. Every price series is converted to the base currency *before* returns are computed

This is the single most important line in the plan, and it is what makes phase 5
correct rather than approximately correct.

```
price_krw(t) = price_native(t) x fx_native_to_krw(t)     # per day, aligned
returns      = daily_returns(price_krw)
```

Doing it in this order means the **existing** covariance / VaR / marginal-risk
machinery in `services/risk_metrics.py` needs no new factor model: the
correlation between a US stock and the exchange rate is discovered empirically
by the covariance matrix, because it is already inside the return series.

Converting *after* computing returns — or converting only the final values —
throws that correlation away, and the correlation is precisely the part that
matters. USDKRW tends to rise when global risk assets fall, so for a Korean
investor USD exposure is a **partial hedge**. Ignoring it does not merely
mis-scale the risk number; it can get the direction wrong.

### 3. Cash is risk-free only in its own currency

KRW cash for a KRW-based investor: volatility zero. **USD cash for the same
investor: full USDKRW volatility**, and correlated with the rest of the book.

So USD cash is not special-cased as "risk-free"; it enters the covariance matrix
as an asset whose return series *is* the USDKRW return series. That falls out of
decision 2 for free and keeps Euler's identity intact.

## What this fixes that is currently broken

* **Mixed-currency valuation is silently wrong today.** `value_holdings`
  (`portfolio_service.py:376-426`) fetches a price per ticker and sums
  `quantity x price` without ever reading `holdings.currency`. Adding
  `005930.KS` puts a won-denominated figure straight into a dollar total — ten
  Samsung shares add 710,000 to `total_market_value`. No error is raised, and
  `risk_metrics` inherits the corrupted weights. **Phase 2 task 1 is a stopgap
  guard for this and should land before anything else.**
* **`risk_metrics.py:386` divides by equity value**, modelling every portfolio as
  100% invested. Holding 50% cash roughly doubles the reported volatility and VaR.
* **`fx_rate` / `currency` / `initial_fx_rate` are dead scaffolding** — written by
  `add_holding` and `record_trade`, read by nothing but one multiplication into
  `total_value`. There is no FX provider and the frontend hardcodes USD.
* **Sells record no realized P/L**, and conversions back to KRW record no
  realized FX gain.
* **Coaching is pre-trade only and is never saved.** A rationale written without
  first pressing *Get coach review* — every trade logged in a hurry, which is to
  say exactly the ones worth reviewing — receives no feedback ever. Phases 8-9.

## A scope boundary worth stating plainly

**The MAS cannot analyze Korean equities.** The fundamental pillar is SEC EDGAR
(`providers/sec_fetch.py`, `providers/edgar_xbrl.py`); there are no filings for a
KOSPI issuer. Consequences:

* `trigger_baseline_if_new` must be **gated to US-listed tickers** (phase 1),
  or every Korean holding starts a doomed background fetch that ends in
  `state: "failed"`.
* Portfolio, cash, valuation, risk, and the coach's behavioural pillar all work
  for Korean holdings. Fundamental analysis and the SEC agent do not.
* The coach must say this rather than silently reasoning from a missing pillar —
  it already appends a `data_limitations` entry when no analysis exists
  (`routers/coach.py:82`), and that path covers it.

DART (금융감독원 전자공시) would be the Korean equivalent and is **out of scope**
here — it is a new provider, a new parser and a new agent, not a phase of this plan.

## Deliberately out of scope

* **Tax lots (FIFO / specific identification).** `_apply_trade`,
  `recompute_average` and the coach's outcome statistics are all built on average
  cost. Note that Korean tax treatment of overseas equity gains is FIFO-based, so
  these figures are for decision-making, not for a tax filing — say so in the UI.
* **Margin, shorts, options.** `side CHECK (side IN ('buy','sell'))` and the
  non-negative quantity rule assume a long-only cash account.
* **Currencies beyond KRW and USD.** The schema is per-currency throughout, so a
  third is a data question rather than a migration, but no phase renders one.
