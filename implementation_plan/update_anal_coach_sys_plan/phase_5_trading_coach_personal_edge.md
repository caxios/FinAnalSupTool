# Phase 5: Trading Coach Evolution (Personal Edge & Behavioral Mirror)

**Goal**: Transform the Trading Coach from an advice-dispensing lecturer into a **Data-Driven Meta-Cognitive Quant Mirror & Rule Incubator** that empowers users to statistically uncover their own personalized trading edge, psychological pitfalls, and custom trading playbook.

---

## Background & Problem

Currently:
1. The Trading Coach evaluates trades primarily through text prompts that judge whether user rationales match AI analyst consensus.
2. Traders do not need generic lectures ("Don't FOMO", "Cut your losses"). They need **objective empirical data about their own past behavior**:
   - What is my actual Win Rate vs Risk-Reward (Payoff Ratio) across different entry strategies?
   - Do I hold losing trades significantly longer than winning trades (Disposition Effect)?
   - Which emotional states (`FOMO`, `Revenge`, `Boredom`) consistently destroy my capital?
   - What is the empirical maximum drawdown (MAE) my winning trades experience, and where is my statistically optimal stop-loss cutoff?
   - What specific combination of conditions constitutes my personal **"Golden Setup"** versus my **"Toxic Pattern"**?

---

## Tasks

### 1. Database Schema Enhancements (`backend/services/db.py`)
- Update `trades` table:
  - Add `emotion_tag TEXT`: nullable string (`'calm'`, `'fomo'`, `'revenge'`, `'boredom'`, `'overconfidence'`, `'fear'`).
  - Add migration logic in `init_db()` using `ALTER TABLE trades ADD COLUMN emotion_tag TEXT` (idempotent).
- Create `trading_rules` table:
  ```sql
  CREATE TABLE IF NOT EXISTS trading_rules (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      rule_type       TEXT NOT NULL CHECK (rule_type IN ('golden', 'toxic', 'custom')),
      title           TEXT NOT NULL,
      conditions_json TEXT NOT NULL,
      description     TEXT NOT NULL,
      win_rate        REAL,
      payoff_ratio    REAL,
      expectancy      REAL,
      is_active       INTEGER NOT NULL DEFAULT 1,
      created_at      TEXT NOT NULL
  );
  ```

### 2. Quantitative Journal & Edge Analytics Engine (`backend/services/journal_analysis.py`)
- **Expectancy ($E$) & Payoff Ratio Matrix**:
  - Compute trade metrics:
    - Win Rate: $W = \frac{N_{\text{wins}}}{N_{\text{total}}}$
    - Payoff Ratio: $R = \frac{\text{Average Gain}}{\text{Average Loss}}$
    - Expectancy: $E = (W \times \text{Avg Gain}) - ((1 - W) \times \text{Avg Loss})$
  - Segment $E$ and $R$ across:
    - Rationale Category: Valuation / Fundamental vs Technical Breakout vs Dip-buy vs Momentum.
    - Emotion Tag: `Calm` vs `FOMO` vs `Revenge` vs `Boredom`.
- **Disposition Effect Tracker**:
  - Compute `avg_holding_days_winners` vs `avg_holding_days_losers`.
  - Calculate Disposition Ratio: $\frac{\text{Avg Holding Days (Losers)}}{\text{Avg Holding Days (Winners)}}$.
  - Flag loss-aversion behavior when ratio $> 2.0$ (holding losing trades twice as long as winners).
- **MAE / MFE Quant Engine (Optimal Stop-Loss & Exit Efficiency)**:
  - Pull historical daily/intraday price action from `executed_at` to position close:
    - **MAE (Maximum Adverse Excursion)**: deepest intra-trade drawdown experienced before sale.
    - **MFE (Maximum Favorable Excursion)**: highest intra-trade peak profit achieved before sale.
  - Compute **Empirical Optimal Stop Loss**:
    - Calculate 90th percentile MAE of winning trades.
    - Insight: *"90% of your profitable trades never dipped below -4.5%. Drops beyond -4.5% have only an 8% recovery rate for your trading style. Optimal stop-loss: -4.5%."*
  - Compute **Exit Efficiency**:
    - Ratio of Realized Gain to MFE. Identifies whether the trader is exiting prematurely (leaving 70%+ of peak gains on the table).
- **Personal Rulebook Synthesizer**:
  - Cluster winning trades into **Golden Setups** (conditions generating top $E$, high payoff, and positive win rate).
  - Cluster losing trades into **Toxic Patterns** (conditions with negative $E$ and outsized drawdowns).

### 3. Backend Coach Router Extensions (`backend/routers/coach.py`)
- Add endpoints:
  - `GET /coach/edge-analytics`: Returns Expectancy matrix, Payoff ratios, MAE/MFE curves, Disposition Effect, and Emotion-PnL breakdown.
  - `GET /coach/rules`: List user's active Golden and Toxic rules.
  - `POST /coach/rules`: Adopt a synthesized rule or create a custom rule.
  - `DELETE /coach/rules/{id}`: Deactivate or remove a rule.
- Enhance `POST /coach/review` (Pre-Trade Review):
  - Check proposed trade against user's active **Toxic Patterns**:
    - If proposed trade matches a toxic setup by $\ge 70\%$ (e.g. `FOMO` tag + ticker up $> 15\%$ in 2 days), trigger a prominent warning banner.
  - Check proposed trade against active **Golden Setups**:
    - Display checklist validation showing alignment with the user's proven edge.

### 4. Trade Entry Emotion Selector (`frontend/src/components/portfolio/TradeForm.tsx`)
- Enhance trade logging form with a 1-click **Emotion Tag Selector**:
  - Options: `😌 Calm/Systematic`, `⚡ FOMO/Rush`, `🔥 Revenge/Impulsive`, `🥱 Boredom`, `🚀 Overconfident`.
  - Seamlessly passed to `POST /portfolio/trades` alongside `entry_rationale`.

### 5. Frontend Personal Trading Edge Dashboard (`frontend/src/components/portfolio/PersonalEdgeDashboard.tsx`)
- Build a dedicated **Personal Trading Edge (🎯)** panel in `views/Portfolio.tsx`:
  - **Expectancy & Payoff Cards**: Win Rate, Payoff Ratio, Net Expectancy ($E$), and Disposition Ratio.
  - **MAE / MFE Curve**: Visual chart indicating the user's empirical optimal stop-loss cutoff.
  - **Emotion vs PnL Heatmap**: Visual bar/matrix showing realized profit/loss across emotional states.
  - **My Trading Playbook**: Interactive list of adopted Golden Rules and Toxic Pattern guardrails with active toggle switches.

---

## Verification & Acceptance Criteria

1. **Schema Migration**:
   - Verify `trades` table accepts `emotion_tag` and `trading_rules` table initializes cleanly on server boot.
2. **Analytics Calculation**:
   - Verify `journal_analysis` returns correct mathematical figures for Expectancy ($E$), Payoff Ratio ($R$), MAE, and MFE given test trades.
3. **Pre-Trade Guardrails**:
   - Adopt a Toxic Rule (e.g., `FOMO` + breakout) $\rightarrow$ initiate a pre-trade review matching those parameters $\rightarrow$ confirm coach displays specific warning referencing user's past empirical loss data.
4. **Interactive Dashboard**:
   - Verify `PersonalEdgeDashboard` renders Expectancy metrics, MAE stop-loss threshold, and emotion heatmaps cleanly in the Portfolio view.
