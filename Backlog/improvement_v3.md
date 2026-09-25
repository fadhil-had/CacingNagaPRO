# Product Requirements Document (PRD)

## CacingNagaPRO — AI Analyst Team

**Version:** v1.0
**Status:** Draft
**Project:** CacingNagaPRO
**Primary Market:** Indonesian Stock Market (IDX)
**Trading Style:** Swing Trading
**AI Runtime:** Hermes Agent
**Data Source:** yfinance + free/open data sources where useful
**Interface:** Telegram
**Core Language:** Python

---

# 1. Product Overview

The CacingNagaPRO AI Analyst Team is a multi-agent system designed to support **daily swing-trading analysis of Indonesian stocks**.

The system consists of four AI analysts:

1. **Market Agent**
2. **Technical Agent**
3. **Flow Agent**
4. **Decision Agent**

The first three analysts perform relatively independent, data-based analyses. The Decision Agent then combines their outputs, identifies conflicts, performs a challenge/review round when necessary, and produces no more than **three stock recommendations**.

Hermes serves as the AI agent runtime and orchestrator, while Python remains the source for market-data calculations and technical indicators.

Telegram serves as the interface for observing the analysis process and final results.

---

# 2. Goals

## Primary Goals

### G1 — Improve screening quality

The system must be able to combine:

* Market conditions
* Technical analysis
* Volume/flow
* Catalysts/news
* Risk/reward

into one consistent decision.

### G2 — Reduce FOMO

The system must not only look for stocks that appear bullish.

The system must be able to produce:

* READY
* WAIT
* REJECT

and explain why a position should not be entered.

### G3 — Produce at most three candidates

Each daily run must produce at most:

```text
TOP 3
```

If no setup meets the required standards, the system may produce:

```text
NO TRADE
```

### G4 — Keep the system simple

The system must not become over-engineered.

Python remains responsible for:

* Data
* Indicator calculations
* Backtesting
* Rule-based calculations

AI is responsible for:

* Interpretation
* Reasoning
* Conflict resolution
* Catalyst interpretation
* Final explanation

---

# 3. Non-Goals

For v1, the system is NOT intended to:

* Trade or execute orders automatically
* Send orders to a broker
* Guarantee profits
* Predict stock prices with certainty
* Know actual trading activity by Indonesian market traders colloquially called `bandar`
* Use paid data
* Replace backtesting
* Use AI to calculate technical indicators from raw OHLC data
* Create dozens of agents

---

# 4. System Architecture

```text
                 CacingNagaPRO
                       │
                       ▼
                ┌──────────────┐
                │ Python Engine│
                └──────┬───────┘
                       │
                  Market Data
                  Indicators
                  Candidates
                       │
                       ▼
                ┌──────────────┐
                │ Hermes Agent │
                │ Orchestrator │
                └──────┬───────┘
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       Market       Technical      Flow
       Agent         Agent         Agent
          │            │            │
          └────────────┼────────────┘
                       ▼
                Decision Agent
                       │
              ┌────────┴────────┐
              │                 │
          Conflict?       No conflict
              │                 │
              ▼                 │
        Challenge Round         │
              │                 │
              └────────┬────────┘
                       ▼
                  Final Result
                       │
                       ▼
                 Telegram Group
```

---

# 5. Agent Specifications

## 5.1 Market Agent

### Objective

Determine the overall market conditions before individual stock candidates are evaluated.

### Input

* IHSG OHLCV
* EMA20
* EMA50
* EMA200
* RSI
* MACD
* ATR/volatility
* Market breadth, if available
* Sector performance, if available

### Output

```json
{
  "regime": "BULLISH | NEUTRAL | BEARISH",
  "confidence": 0,
  "swing_environment": "FAVORABLE | NEUTRAL | UNFAVORABLE",
  "reason": [],
  "risk_flags": []
}
```

### Example

```text
MARKET REGIME: NEUTRAL-BULLISH

Swing Environment: FAVORABLE

Reasons:
- IHSG above EMA20
- Momentum improving
- Volatility moderate

Risk:
- Resistance nearby
```

---

# 6. Technical Agent

## Objective

Analyze the price structure and technical setup of each candidate.

## Input

Python provides the calculated values for:

* EMA20
* EMA50
* EMA200
* RSI
* MACD
* ADX
* ATR
* Bollinger Bands
* Volume
* Support
* Resistance
* Recent highs/lows
* Price structure

## Recognized Setups

* Breakout
* Pullback
* Reversal
* Trend continuation
* Range
* Failed breakout
* Breakdown

## Output

```json
{
  "ticker": "ERAA",
  "trend": "BULLISH",
  "setup": "PULLBACK",
  "momentum": "POSITIVE",
  "support": [],
  "resistance": [],
  "entry_zone": [],
  "technical_score": 0,
  "invalidation": 0,
  "reason": []
}
```

The AI does not calculate indicators.

The AI only interprets the results calculated by Python.

---

# 7. Flow Agent

## Objective

Analyze indications of accumulation or distribution using available data.

## Input

* Volume
* Relative volume
* OBV
* MFI
* CMF
* Price-volume relationship
* Volume anomaly
* Up/down volume
* Foreign flow, if a free source is available
* Broker flow, if a free source is available

## Output

```json
{
  "ticker": "ERAA",
  "flow": "ACCUMULATION | NEUTRAL | DISTRIBUTION",
  "strength": "STRONG | MEDIUM | WEAK",
  "confidence": 0,
  "evidence": [],
  "risk": []
}
```

### Important Rule

The agent **must not state as a fact that Indonesian market traders colloquially called `bandar` are buying**.

Use terms such as:

* Accumulation likely
* Distribution risk
* Positive flow indication
* Weak accumulation
* Neutral flow

based on the available evidence.

---

# 8. Decision Agent

## Objective

Act as the final decision maker.

The Decision Agent receives the outputs of:

* Market Agent
* Technical Agent
* Flow Agent
* Candidate data
* News/catalysts, if available
* Risk/reward calculation

## Responsibilities

1. Combine the agent outputs.
2. Identify conflicts.
3. Perform a challenge round when necessary.
4. Determine the status.
5. Calculate/validate risk/reward.
6. Rank candidates.
7. Produce no more than three recommendations.

---

# 9. Decision States

Every stock must have one of the following statuses:

### READY

The setup meets the requirements and offers a reasonable risk/reward ratio.

### WAIT

The setup is attractive but requires confirmation or a pullback.

### REJECT

The setup does not meet the requirements.

### NO TRADE

No candidate meets the required standards.

---

# 10. Challenge Mechanism

The Decision Agent must not immediately accept all agent outputs.

If a significant conflict occurs:

```text
Technical = Bullish
Flow = Distribution
Market = Bullish
```

the Decision Agent initiates a challenge.

Example:

```text
DECISION AGENT:

Technical Agent:
Explain why the bullish setup remains valid.

Flow Agent:
Explain the evidence supporting distribution risk.
```

The agents provide their evidence.

The Decision Agent then produces a final conclusion.

---

# 11. Catalysts / News

News will not be implemented as a fifth agent.

For v1, the **Decision Agent** may perform a catalyst assessment using available data and news.

The assessment looks for:

* Corporate actions
* Contracts
* Earnings
* Acquisitions
* Regulations
* Sector catalysts
* Significant company announcements
* Material negative news

Output:

```json
{
  "catalyst": "POSITIVE | NEUTRAL | NEGATIVE",
  "impact": "HIGH | MEDIUM | LOW",
  "freshness": "RECENT | OLD | UNKNOWN",
  "reason": []
}
```

News must not automatically override technical or risk considerations.

---

# 12. Risk Management

Target strategy:

```text
Take Profit:
3% – 10%

Stop Loss:
2% – 5%

Maximum holding period:
10 trading days
```

The Decision Agent must consider:

* Entry zone
* Stop loss
* TP1
* TP2
* Risk/reward
* Support
* Resistance
* ATR
* Setup invalidation

Example output:

```text
ERAA

Entry: 875–890
SL: 845
TP1: 930
TP2: 970
Maximum Hold: 10 trading days
```

---

# 13. Candidate Selection

Python performs the initial screening first.

The AI does not need to analyze every IDX stock unless necessary.

Pipeline:

```text
IDX Universe
     ↓
Python Screener
     ↓
Candidate Pool
     ↓
AI Agents
     ↓
Decision Agent
     ↓
Top 3
```

Initial screening may use:

* Liquidity
* Price range
* Volume
* Technical setup
* Recent momentum
* Basic volatility

---

# 14. Ranking

The final ranking uses a combination of a deterministic score and AI interpretation.

Example:

```text
Technical       30%
Flow            20%
Market Fit      15%
Risk/Reward     20%
Catalyst        10%
Liquidity        5%
```

These weights must be configurable and changeable without modifying agent code.

The AI must not rank candidates solely based on intuition.

---

# 15. Final Output

Telegram must produce a report such as:

```text
🧠 CACINGNAGAPRO DAILY ANALYSIS

Date:
25 September 2026

Market:
NEUTRAL-BULLISH

Swing Environment:
FAVORABLE


🏆 TOP 3


🥇 ERAA

Status: READY

Setup:
Pullback

Entry:
875–890

Stop Loss:
845

TP1:
930

TP2:
970

Hold:
≤10 trading days

Why:
• Trend bullish
• Price near support
• Momentum positive
• Flow moderately positive

Risk:
• Resistance nearby
• Flow confirmation not strong


🥈 CUAN

...


🥉 DEWA

...


⚠️ MARKET RISK

...


❌ REJECTED / WAIT

INET — WAIT
Reason:
Breakout not sufficiently confirmed.
```

---

# 16. Telegram Interaction

Telegram functions as a **visualization/interface**, not as the primary database.

Recommended commands:

```text
/screen
```

Runs the daily screening process.

```text
/analyze ERAA
```

Requests analysis of one stock.

```text
/market
```

Displays the current market regime.

```text
/status
```

Displays the latest pipeline status.

```text
/debate ERAA
```

Requests that the agents perform a challenge/review for ERAA.

```text
/why ERAA
```

Displays the reasoning behind the final decision.

---

# 17. Telegram Group

Recommended setup:

```text
CacingNagaPRO AI Trading Room
```

The bot may display:

```text
🧠 Market Agent
📈 Technical Agent
🐋 Flow Agent
🎯 Decision Agent
```

However, the agents do not need to send messages at all times.

Default:

```text
/screen
    ↓
analysis
    ↓
final report
```

If a significant conflict exists, the discussion/debate is shown.

---

# 18. Data Architecture

Python remains the source of truth for all numerical data.

```text
yfinance
   ↓
Data Collector
   ↓
Indicator Engine
   ↓
Candidate Screener
   ↓
Structured JSON
   ↓
Hermes
```

The AI receives structured data rather than raw dataframes unless raw data is necessary.

Example:

```json
{
  "ticker": "ERAA",
  "price": 885,
  "ema20": 870,
  "ema50": 830,
  "rsi": 62,
  "atr": 28,
  "support": 850,
  "resistance": 950,
  "relative_volume": 1.42,
  "obv_trend": "UP",
  "cmf": 0.18
}
```

---

# 19. Reliability Rules

Agents must:

1. Never invent data.
2. Never invent news.
3. Never invent broker-flow data.
4. Never claim certainty about actual transactions by Indonesian market traders colloquially called `bandar`.
5. Report `UNKNOWN` when data is unavailable.
6. Never assign high confidence when evidence is weak.
7. Never change numerical values calculated by Python.
8. Never ignore a stop loss merely because the thesis is bullish.
9. Never force three recommendations when there are not enough qualified candidates.

---

# 20. Backtesting Integration

AI outputs must be persisted for evaluation.

Each recommendation must store:

```text
Date
Ticker
Market Regime
Setup
Entry
SL
TP1
TP2
Agent Outputs
Final Decision
Confidence
Actual Result
Maximum Favorable Excursion
Maximum Adverse Excursion
Holding Period
```

The goal is not merely to determine whether a stock generated a profit.

We also want to determine:

```text
Was the Technical Agent correct?
Did the Flow Agent add value?
Did the Market Agent improve filtering?
Does the Decision Agent issue WAIT decisions too frequently?
```

---

# 21. Performance Metrics

Minimum tracking requirements:

### Signal metrics

* Win rate
* Average return
* Median return
* TP hit rate
* SL hit rate
* Average holding period

### Risk metrics

* Maximum drawdown
* Average loss
* Average win
* Risk/reward
* Profit factor

### Agent metrics

* Technical signal success
* Flow confirmation success
* Market regime effectiveness
* Decision Agent success

### Benchmarks

Compare against:

```text
IHSG
Buy & Hold
Random candidate baseline
Current CacingNagaPRO screener
```

---

# 22. MVP Scope

The MVP requires only:

### Data

* yfinance
* IHSG
* IDX stock universe

### Agents

* Market Agent
* Technical Agent
* Flow Agent
* Decision Agent

### Interface

* Telegram

### Core functionality

```text
/screen
/analyze TICKER
/market
/why TICKER
```

### Excluded from the MVP

* Automated trading
* Paid data
* Broker APIs
* Complex memory system
* More than 10 agents
* Autonomous trading
* Position sizing
* Portfolio management

---

# 23. Success Criteria

The MVP is considered successful if:

### Functional

* Daily screening can run automatically.
* All four agents produce valid outputs.
* The Decision Agent can resolve conflicts.
* No more than three stocks are recommended.
* Telegram receives the results.
* Every numerical value comes from Python or the original data source.

### Analytical

After collecting enough samples, the system must be evaluated to determine whether:

* AI filtering improves screener results.
* Flow analysis provides incremental value.
* Market-regime analysis helps avoid poor setups.
* The Decision Agent does not increase overtrading.

A profit target must not be presented as a guarantee of system success.

---

# 24. Development Phases

## Phase 1 — Foundation

```text
Python data pipeline
+
Indicator engine
+
Candidate screener
```

## Phase 2 — AI Agents

```text
Market Agent
Technical Agent
Flow Agent
Decision Agent
```

## Phase 3 — Hermes Integration

```text
Python → Hermes
Hermes → Agents
Agents → Decision
```

## Phase 4 — Telegram

```text
/screen
/analyze
/market
/why
```

## Phase 5 — Debate

Add the challenge mechanism for significant conflicts.

## Phase 6 — Evaluation

Persist all outputs and compare them with the existing CacingNagaPRO backtest.

---

# 25. Design Principle

The core principle of CacingNagaPRO AI is:

> **Python calculates.
> Agents interpret.
> Decision Agent challenges.
> Backtest judges.**

The AI must not be the source of truth for numerical data.

Market data and calculations must remain deterministic.

AI is used to provide **interpretation, context, contradiction checking, and synthesis**.

---

# 26. Final Product Vision

CacingNagaPRO is more than:

```text
Ticker → Score → BUY
```

It is:

```text
Market
   ↓
Technical
   ↓
Flow
   ↓
Conflict Detection
   ↓
Challenge
   ↓
Risk Assessment
   ↓
Decision
   ↓
TOP 3 / WAIT / NO TRADE
   ↓
Backtest
```

The ultimate goal is to build an **auditable AI trading analyst team**, not a black-box stock picker.
