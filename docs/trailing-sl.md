# Trailing stop-loss methods

Reference for the Stage 4 trailing-SL engine (PRD §6.2, §11 S4; decisions D-006, D-007, D-025).
This is planning material: the exact rules, parameters and tests are fixed in the Stage 4
OpenSpec change (`add-trailing-exits`), not here.

## What a trailing stop is

A **stop-loss (SL)** is the price at which a losing position is closed. A **trailing** SL moves
in the trade's favour as the price does (up for a long, down for a short) and never moves back.
It locks in profit while letting a trend run. The methods below differ only in *how* they
decide where the stop should be.

In SentinelX the trailing level lives **only in the engine** (D-006). Dhan sees one entry, one
wide **catastrophic SL** placed at the exchange as a worst-case backstop (D-007), and one exit
order when the engine's trail is hit. The method choice changes none of that.

## Method catalogue

The scores and win-rate tendencies are the owner's working assessment, **not measured results**.
They are hypotheses that the local backtester must confirm per instrument and timeframe before
a method is relied on (D-003, D-011).

| # | Method | Score /10 | Win-rate tendency |
|---|---|---|---|
| 1 | ATR Chandelier | 8.5 | Medium |
| 2 | 5-candle lowest low | 8 | Medium |
| 3 | Supertrend | 7.5 | Low–Medium |
| 4 | Step trail | 7 | Medium–High |
| 5 | Parabolic SAR | 6.5 | Medium |
| 6 | 5th-candle-back low | 6 | Medium |
| 7 | EMA close trail | 6 | Low |
| 8 | Fixed % | 5.5 | Medium |

## Which one to use

| Situation | Best choice | Why |
|---|---|---|
| Strong, steady trend (daily/weekly) | ATR Chandelier or Supertrend | Wide enough to survive normal pullbacks, and adapts to volatility |
| Trending but choppy, clear swing highs and lows | 5-candle lowest low | Follows real market structure, so you exit only when a swing low breaks |
| Intraday (5m/15m), quick moves | 5-candle low or Parabolic SAR | Tight and fast, which suits short holding times |
| Breakout trade likely to move fast, then stall | Parabolic SAR | Accelerates to lock profit near the top of the burst |
| Small, steady target (e.g. 2–6%), low-volatility stock or index | Step trail | Gives breakeven early, then locks gains in fixed chunks |
| Long-term holding (positional, weeks to months) | EMA close trail (20 or 50 EMA) | Ignores daily noise and only exits when the trend actually breaks |
| Beginner, need something simple to follow manually | Step trail or Fixed % | Easy to calculate without a chart |

Note: SentinelX's primary market is NSE F&O **intraday** (PRD header), where positions are
squared off the same day (PRD §7, own square-off 15:10 IST). The daily/weekly and positional
rows apply to equity holdings, which are outside Stage 4's intraday scope.

## How each method computes the stop

Written for a **long** position. A **short** mirrors every rule: highest high instead of lowest
low, stop *above* price, plus instead of minus. Typical parameter values are starting points for
backtesting, not recommendations.

Terms used below:
- **Candle (bar):** open/high/low/close prices over one timeframe, e.g. 5 minutes.
- **ATR (Average True Range):** the average size of recent candles' ranges, a measure of
  volatility. A wider ATR means a wider stop.
- **EMA (Exponential Moving Average):** an average of closing prices that weights recent
  candles more.
- **Swing low:** a local bottom in price; a break below it signals the up-move has ended.

| Method | Stop level (long) | Typical parameters | Needs | Exit when |
|---|---|---|---|---|
| ATR Chandelier | highest high since entry − k × ATR(n) | n = 22, k = 3 | candles, ATR | price trades at or below the stop |
| 5-candle lowest low | lowest low of the last N completed candles | N = 5 | candles | price trades at or below the stop |
| Supertrend | Supertrend lower band (median price − k × ATR(n), ratcheting up only) | n = 10, k = 3 | candles, ATR | a candle closes below the band |
| Step trail | at +X profit move SL to breakeven; then raise it by S for every further S of profit | X, S in points, % or R* | ticks | price trades at or below the stop |
| Parabolic SAR | SAR value, which accelerates toward price on each new high | AF 0.02, step 0.02, max 0.2 | candles | price trades at or below the SAR |
| 5th-candle-back low | low of the candle exactly N candles ago (one candle, not the lowest of N) | N = 5 | candles | price trades at or below the stop |
| EMA close trail | EMA(n) of closes | n = 20 or 50 | candles, EMA | a candle **closes** below the EMA |
| Fixed % (or fixed points) | highest price since entry × (1 − p%), or − fixed points | p = 1–3% | ticks | price trades at or below the stop |

\* **R** is the initial risk per unit, i.e. the distance from entry to the initial stop.
"+1R" means the trade has gained as much as it risked.

"Exit when price trades at or below" is checked on every tick against the engine's level (PRD
§6.2), so these exits are fast. The two **close-based** methods (Supertrend, EMA close trail)
deliberately ignore intra-candle dips and act only when a candle closes beyond the line; that is
what makes them tolerant of noise, and also what makes their exits later.

## Rules that apply to every method

1. **Tighten only.** The stop only moves in the trade's favour. A computed level that is looser
   than the current one is ignored.
2. **Never looser than the initial stop.** Position size is calculated from the initial SL
   distance (PRD §7, per-trade max risk). The trail may start at that stop, never below it.
3. **Catastrophic SL stays at the exchange** and is cancelled only after the exit fills (D-007).
4. **No per-tick order changes at Dhan** (D-006). Rate limits and the 25-modification cap make
   that impossible anyway (PRD §10).
5. **Restart-safe.** The method, its parameters, the current level and any internal state (for
   example Parabolic SAR's acceleration factor and extreme point, or the highest high so far)
   are persisted, so a restarted engine resumes the same trail (PRD §6.5).
6. **The engine never picks a method on its own.** A person (per trade) or a strategy (Stage 9+)
   chooses; the engine only applies it.

## How the method is chosen (D-025)

- A **default method and parameters** live in configuration, stored and edited like the risk
  limits (PRD §7).
- The **trade panel** (Stage 6) shows the default and lets the trader pick another method and
  its parameters **per trade**, before submitting. Until then, the developer CLI does the same.
- **Strategies** (Stage 9+) send their own method and parameters with each signal.
- Changing the method of an **open** position is allowed only if the new stop is at least as
  tight as the current one (same tighten-only rule as risk limits during market hours, D-012).

## Data the methods need (impacts Stage 1)

Seven of the eight methods need **candles** (and some need ATR or EMA computed from them); only
Fixed % and Step trail work from ticks alone. So:

- the market-data stage must build candles from the tick feed for the timeframes in use
  (for example 1m, 5m, 15m);
- a trade needs enough history at entry (for example 22 candles for ATR(22)), so recent
  historical candles must be loaded to warm the indicators up rather than waiting after entry;
- indicators must be computed on **completed** candles only, so the stop does not jitter within
  a forming candle.

## Open questions for the Stage 4 proposal

- Which timeframes to support first (proposal: 1m, 5m, 15m).
- Whether `highest high since entry` (Chandelier) uses candle highs or tick highs.
- Whether Step trail and Fixed % steps are in points, %, or R, or all three.
- Behaviour when history is too short to compute the indicator at entry (proposal: reject the
  method for that trade with a clear reason, never fall back silently).
