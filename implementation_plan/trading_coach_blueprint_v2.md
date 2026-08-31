# AI Trading & Portfolio Coach System Blueprint

This document outlines the architecture and implementation plan for the AI Trading & Portfolio Coach System. It synthesizes quantitative risk metrics, deep fundamental analysis, and user trading psychology.

## 1. Automated Portfolio & Trading Journal System
*   **Initial Portfolio Setup**: The user inputs basic data for existing holdings: `Ticker`, `Average Price`, `Quantity`, and `Initial Exchange Rate` (if applicable).
*   **Smart Trading Journal Automation**: 
    *   When logging a new trade, the user only inputs the **Transaction Time** and **Quantity**.
    *   The system uses a financial API (e.g., `yfinance` intraday data) to automatically calculate the exact `Execution Price`, `Total Transaction Value`, `Updated Portfolio Average Price`, and `Real-time ROI`.
*   **Dedicated "Entry Rationale" Field**: 
    *   The UI must include a specific, separate text field labeled "Entry Rationale" (진입 이유). 
    *   The user explicitly documents their trading logic and psychological state (e.g., "Bought because of FOMO", "Technicals broke out"). 
    *   The Coach Agent will specifically extract this field to evaluate the user's subjective logic against objective AI data.

## 2. Tool-Augmented Quantitative Risk Agent
A specialized agent equipped with Python mathematical tools (NumPy, Pandas, SciPy) to calculate objective portfolio risk metrics before qualitative assessment.
*   **Mandatory Calculations**:
    *   `Value at Risk (VaR)` and `Conditional VaR (Expected Shortfall)`
    *   `Portfolio Volatility` and `Max Drawdown`
    *   `Individual Stock Volatility` and `Correlation Matrix`
    *   `Marginal Risk Contribution`: Quantifies exactly how adding a new stock (or increasing position size) alters the total portfolio risk.
*   **Synthesis**: The agent merges these hard mathematical outputs with the Macro Market Agent's qualitative regime assessment (e.g., "High-rate environment") to issue a holistic risk warning or validation.

## 3. Adaptive & Holistic Trading Coach Agent
This agent acts as a personalized meta-cognitive coach, helping the user reinforce good habits through feedback loops.
*   **Data Synthesis**: It merges three pillars of data:
    1.  `Fundamental Agent Data`: Intrinsic value, cash flow trends, SEC filing deep dives.
    2.  `Technical Agent Data`: Price action, momentum, moving averages, entry/exit validity.
    3.  `User's Historical Journal`: The user's past trades and their stated "Entry Rationales".
*   **Bias Detection & Coaching**: The agent evaluates the user's "Entry Rationale" against the AI's objective data. It identifies psychological biases (e.g., Panic selling, FOMO) and provides reinforcement learning feedback based on the user's own historical mistakes (e.g., "You stated you are selling due to broken technicals, but fundamentals are up 20%. In your last 3 similar trades, this pattern led to missing a 15% rebound. Reconsider your rationale.").

## 4. 8-Quarter Fundamental Baseline Automation
To ensure the Coach Agent has a solid foundation for its advice:
*   **Auto-Trigger**: Whenever a user adds a new ticker to their portfolio, the system automatically fetches the SEC filings (10-K, 10-Q) and Earnings Call transcripts for the **past 8 quarters (2 years)**.
*   **Baseline Debate**: The MAS runs a fundamental debate over this entire 2-year history to generate a comprehensive "Baseline Fundamental Report". This prevents the coach from being swayed by short-term volatility and anchors its advice in long-term corporate health.
