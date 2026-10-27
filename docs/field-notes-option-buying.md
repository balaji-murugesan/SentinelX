# Field notes: intraday NIFTY option buying (external, unverified)

Source: a Reddit post by an independent algo trader (copied by the owner, 2026-10-28). Nothing
here is measured by us. Every number is the author's own claim from his own backtests, on his
own data and period, and is a **hypothesis for the local backtester** (D-003, D-005), not a
parameter to hard-code. Strategy research stays outside this repo; this note records only what
touches the engine.

## What the author claims

- **Low win rate, big winners.** 1,396 NIFTY option-buying trades over ~11 years, ~45% win
  rate, profitable because winning trades are 2–3× the size of losing ones.
- **Stop and trail.** Best zone on NIFTY option buying: SL at **30% of the option premium**;
  shift the SL once the trade is **+20%** on premium.
- **Gaps drive the edge.** Days opening ≥ 0.75% away from the previous close earned ~3× the
  normal day's average trade. Profit came from gaps that continued; losses from gaps that
  reversed.
- **One setup to skip.** Small gap (0.2–0.5%) after a close ≥ 2.5% above the 20-day average:
  13% win rate over 67 trades, held up out of sample. Reversing it also lost — skip it.
- **No calendar edge.** Month, season, Budget day, elections, festivals: no real edge once
  tested properly. A big month does not predict a bad month next.
- **Costs kill stock options.** Stock options cost ~0.25–1% per side vs ~0.1–0.2% for NIFTY
  options; stock gap trades lost money after costs.
- **The edge is shrinking.** ₹2,394 average per trade over 2015–26, only ₹860 over 2023–26.

Caveats: a single trader's results, one market regime, many filters tried (so some "findings"
are expected by chance — the 67-trade setup is a small sample), and no independent check.

## What it means for SentinelX

| Claim | Stage | Engine impact |
|---|---|---|
| SL 30% of premium, shift at +20% | S4 (D-025) | Covered by fixed % + step trail; worth a named preset. Raises the open question of which price the trail follows — option premium or the underlying (PRD §12 Q9) |
| 45% win rate, 2–3× payoff | S6, S10 | Report **expectancy** (average P&L per trade) and **payoff ratio** (avg win ÷ avg loss), not just win rate. Engine computes, UI displays |
| Costs decide whether an edge survives | S2 | `PaperBroker` must model brokerage, STT, exchange and SEBI fees, stamp duty and GST, not only slippage — otherwise paper P&L overstates live P&L |
| Edge decays over time | S10 | Track rolling expectancy per strategy; alert, and optionally pause the strategy, when it falls below a configured floor |
| Gap size and skip-filters | S9 | Strategy logic, built and tested in the backtester and sent as signals. Engine only needs previous close available (Dhan feed sends a `PREV_CLOSE` packet, currently ignored) |
| No calendar edge | — | Do not build event-day filters speculatively |

Not adopted: Kite / FYERS (Zerodha stays manual, D-022); one Telegram bot per strategy (one
engine-owned bot for alerts and `/kill`, D-021 — tag alerts with the strategy instead).

## Expectancy, briefly

Expectancy per trade = (win rate × average win) − (loss rate × average loss).
At a 45% win rate with wins 2.5× losses, risking 1 unit per trade:
0.45 × 2.5 − 0.55 × 1 = **+0.575 units per trade**. Round-trip costs come straight out of that
figure, which is why a thin edge can vanish on expensive instruments.
