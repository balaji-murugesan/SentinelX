"""The real ``RiskGate``: every order passes through ``RiskEngine.check`` before it can reach the
broker (add-risk-engine task 5.3; risk-gate spec; design §2, §3). It replaces the Stage 2 stub
(a paper-only gate, since deleted) behind the same ``RiskGate`` Protocol, so no call
site changed.

What it decides, in order:

  1. **Live mode stays closed.** While no live broker exists, every order in live mode is refused
     with ``RISK_LIVE_NOT_ENABLED`` (spec "Live mode stays closed until a live broker exists").
  2. **Entry or exit?** ``engine.orders.intent.classify`` decides from the position alone.
  3. **An exit** is accepted unless a rule that applies to exits (price sanity) refuses it. Entry
     rules, halts, the day lock and a missing or unreadable risk state never refuse an exit: the
     engine must always be able to reduce risk (safety rule 9). If the exit cannot be judged at
     all (no limits, no state), it is accepted and the audit says why it was not judged.
  4. **An entry** is refused, fail closed, when the gate is not connected to trading state, the
     limits are missing or unusable, the risk state is unavailable (Postgres lost, the monitor
     stalled), the day is locked, or a standing halt such as a silent feed applies. Only then do
     the rules run, all of them, each in ``enforce`` or ``shadow`` mode, and the most severe
     enforced decision wins (``engine.risk.decision``).

Every verdict is audited, including ALLOW, plus one row for each rule that said anything other
than a plain ALLOW (shadow rules included, with ``enforced = False``). The audit goes through
``RiskAudit``, which never raises, so a failed or malformed audit record cannot block an exit.

Binding (design §3): ``OrderService`` needs a gate at construction and the gate needs the
service's orders and positions. The gate is built first, ``bind`` is called once the service
exists, and until then every order counts as an entry that is refused. The gate runs inside the
service's lock, so what it reads is consistent.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Final, Protocol

import structlog

from contracts.market import Instrument, InstrumentKey
from contracts.mode import TradingMode
from contracts.orders import Order, Position
from contracts.snapshot import HaltLevel
from engine.market_data.clock import utc_now
from engine.orders.intent import Intent, classify
from engine.positions.book import PositionId
from engine.risk.audit import RiskAudit
from engine.risk.decision import (
    CHECK_ERROR,
    HALTED_DAY,
    LIVE_NOT_ENABLED,
    Decision,
    Phase,
    PreTradeCheck,
    Quote,
    RiskContext,
    RuleResult,
    Severity,
    most_severe,
    run_pre_trade,
)
from engine.risk.limits import STATE_UNAVAILABLE
from engine.risk.limits_service import LimitsService
from engine.risk.repository import AuditValue, RiskEvent
from engine.risk.state import DayState

log = structlog.get_logger("engine.risk.gate")

STANDING_HALT: Final = "standing_halt"  # the rule id of a self-clearing halt of new entries
GATE: Final = "gate"  # the rule id on verdict rows
VERDICT: Final = "verdict"  # their phase


class TradingView(Protocol):
    """The two read-only maps of trading state that the gate reads: ``OrderService`` has both.
    They are properties so that the service may replace its dicts (``restore``) and the gate
    still sees the current ones. A ``Protocol`` attribute is satisfied by a plain attribute."""

    @property
    def positions(self) -> Mapping[PositionId, Position]: ...

    @property
    def orders(self) -> Mapping[str, Order]: ...


class RiskState(Protocol):
    """What the gate reads of the risk engine's live state. The real implementation is the day
    book and monitor (add-risk-engine groups 6 and 7); tests use a small fake."""

    def unavailable(self, now: datetime) -> str | None:
        """Why entries must be refused as ``RISK_STATE_UNAVAILABLE`` (state not loaded, persistence
        lost, the monitor has stopped beating), or None when the state can be trusted."""
        ...

    def day(self) -> DayState:
        """Today's state: counters, halt level, peak and floor."""
        ...

    def mtm_day(self) -> Decimal | None:
        """The day's P&L, or None while it is unknown."""
        ...

    def quote(self, key: InstrumentKey) -> Quote | None:
        """The latest price of ``key`` and its age, or None if it has none."""
        ...

    def entry_halt(self) -> Decision | None:
        """A standing, self-clearing halt of new entries (a silent feed, an unknown MTM, an
        unprotected position), or None."""
        ...

    async def entry_allowed(self) -> str | None:
        """Count one allowed entry and persist the count before the entry is sent. None on
        success; otherwise why it could not be saved, and the entry is refused."""
        ...


class RiskEngine:
    def __init__(
        self,
        *,
        account_id: str,
        mode: TradingMode,
        limits: LimitsService,
        state: RiskState,
        audit: RiskAudit,
        instruments: Callable[[InstrumentKey], Instrument | None],
        checks: Sequence[PreTradeCheck],  # required: a gate with no rules would allow everything
        on_kill: Callable[[Decision], None] | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """``on_kill`` is called when an enforced pre-trade rule returns KILL, so that the engine
        acts on the most severe decision wherever it was seen (the floor check is normally a
        continuous one, but a pre-trade rule may return KILL too). The gate runs inside the order
        service's lock, which the kill's own cancels need, so the hook must only *schedule* the
        kill and return (``KillSwitch.request``); it must never await it."""
        self._account_id = account_id
        self._mode = mode
        self._limits = limits
        self._state = state
        self._audit = audit
        self._instruments = instruments
        self._checks = tuple(checks)
        self._on_kill = on_kill
        self._now = now
        self._trading: TradingView | None = None

    def bind(self, trading: TradingView) -> None:
        """Connect the gate to trading state (design §3). Called once, by ``engine.main``."""
        self._trading = trading

    async def check(self, order: Order) -> str | None:
        """None to let ``order`` through, else the reason it is refused (final)."""
        started = time.perf_counter()
        now = self._now()
        intent: Intent | None = None  # None until the order has been classified
        results: list[RuleResult] = []
        extra: dict[str, AuditValue] = {}
        try:
            if self._mode is not TradingMode.PAPER or not order.paper:
                reason: str | None = f"{LIVE_NOT_ENABLED}: live orders are not enabled yet"
            elif self._trading is None:
                reason = f"{STATE_UNAVAILABLE}: the risk engine is not connected to trading state"
            else:
                intent = classify(order, self._trading.positions)
                if intent is Intent.EXIT:
                    reason = self._judge_exit(order, now, results, extra)
                else:
                    reason = self._judge_entry(order, now, results, extra)
                    if reason is None and (saved := await self._state.entry_allowed()) is not None:
                        # Every rule allowed it, but the day's entry count could not be written
                        # first: refuse (fail closed), because an uncounted entry could breach
                        # the daily maximum after a restart.
                        reason = f"{STATE_UNAVAILABLE}: {saved}"
        except Exception as error:
            # Nothing above should raise (the checks are guarded), but if it does: an entry
            # fails closed and an exit goes through, exactly as the spec's rule for unavailable
            # state.
            log.error("risk_gate_failed", order_id=order.order_id, exc_info=True)
            extra["gate_error"] = type(error).__name__
            reason = (
                None
                if intent is Intent.EXIT
                else f"{STATE_UNAVAILABLE}: the risk gate failed ({type(error).__name__})"
            )
        await self._record(order, intent, reason, results, extra, started, now)
        return reason

    # ---- exits -----------------------------------------------------------------------------

    def _judge_exit(
        self, order: Order, now: datetime, results: list[RuleResult], extra: dict[str, AuditValue]
    ) -> str | None:
        """Accept unless a rule that applies to exits refuses it. A rule that *crashes* does not
        refuse an exit: the engine must be able to reduce risk, so the crash is audited and the
        exit goes through."""
        unjudged = self._why_unjudged(now)
        if unjudged is not None:
            extra["skipped"] = unjudged  # accepted without being judged, and the audit says so
            return None
        ctx = self._context(now)
        results.extend(run_pre_trade([c for c in self._checks if c.applies_to_exits], ctx, order))
        winner = most_severe(r for r in results if r.decision.code != CHECK_ERROR)
        self._request_kill_if_severe(winner)
        return None if winner is None else self._refusal(winner, extra)

    def _request_kill_if_severe(self, winner: RuleResult | None) -> None:
        if winner is None or winner.effective < Severity.KILL or self._on_kill is None:
            return  # nothing severe, or a shadow rule (its effective severity is lower)
        try:
            self._on_kill(winner.decision)
        except Exception:  # the order is refused either way; the kill is retried by the monitor
            log.critical("kill requested by a pre-trade rule could not be scheduled", exc_info=True)

    def _why_unjudged(self, now: datetime) -> str | None:
        if self._limits.current is None:
            return self._limits.reason_for() or "limits are not available"
        return self._state.unavailable(now)

    # ---- entries ---------------------------------------------------------------------------

    def _judge_entry(
        self, order: Order, now: datetime, results: list[RuleResult], extra: dict[str, AuditValue]
    ) -> str | None:
        if (limits_reason := self._limits.reason_for()) is not None:
            return limits_reason
        if (unavailable := self._state.unavailable(now)) is not None:
            return f"{STATE_UNAVAILABLE}: {unavailable}"
        day = self._state.day()
        if day.halt_level > HaltLevel.NONE:
            return f"{HALTED_DAY}: {day.halt_reason or 'the day is locked'}"
        if (halt := self._state.entry_halt()) is not None:
            # Recorded as a rule result of its own, so the audit shows the halt and the verdict
            # carries its severity.
            results.append(RuleResult(STANDING_HALT, Phase.CONTINUOUS, halt, True, 0))
            extra["decided_by"] = STANDING_HALT
            return halt.reason() or f"{STATE_UNAVAILABLE}: new entries are halted"
        results.extend(run_pre_trade(self._checks, self._context(now), order))
        winner = most_severe(results)
        self._request_kill_if_severe(winner)
        return None if winner is None else self._refusal(winner, extra)

    @staticmethod
    def _refusal(winner: RuleResult, extra: dict[str, AuditValue]) -> str:
        extra["decided_by"] = winner.rule
        return winner.decision.reason() or f"{winner.decision.severity.name}: {winner.rule}"

    def _context(self, now: datetime) -> RiskContext:
        limits, trading = self._limits.current, self._trading
        assert limits is not None and trading is not None  # both ensured by the callers
        return RiskContext(
            account_id=self._account_id,
            now=now,
            limits=limits,
            day=self._state.day(),
            mtm_day=self._state.mtm_day(),
            positions=trading.positions,
            orders=trading.orders,
            instruments=self._instruments,
            quote=self._state.quote,
        )

    # ---- audit -----------------------------------------------------------------------------

    async def _record(
        self,
        order: Order,
        intent: Intent | None,
        reason: str | None,
        results: list[RuleResult],
        extra: dict[str, AuditValue],
        started: float,
        now: datetime,
    ) -> None:
        """Audit the verdict and every rule that said something. Each record is built and written
        inside its own guard: a malformed record (or a failed write) is logged and never reaches
        the caller, and it does not stop the other records (the verdict row in particular)."""

        async def write(build: Callable[[], RiskEvent]) -> None:
            try:
                await self._audit.record(build())
            except Exception:
                log.error("risk_audit_not_built", order_id=order.order_id, exc_info=True)

        for result in results:
            if result.decision.severity > Severity.ALLOW or result.decision.code is not None:
                await write(partial(self._rule_event, order, result, now))
        await write(
            partial(self._verdict_event, order, intent, reason, results, extra, started, now)
        )

    def _verdict_event(
        self,
        order: Order,
        intent: Intent | None,
        reason: str | None,
        results: list[RuleResult],
        extra: dict[str, AuditValue],
        started: float,
        now: datetime,
    ) -> RiskEvent:
        return RiskEvent(
            at=now, account_id=self._account_id, rule=GATE, phase=VERDICT,
            decision=_verdict_severity(reason, results).name, enforced=True,
            latency_us=int((time.perf_counter() - started) * 1_000_000),
            reason_code=None if reason is None else reason.split(":", 1)[0],
            inputs={
                "order_id": order.order_id,
                # An order refused before it could be classified (live mode, an unbound gate)
                # is not an entry: say so, rather than claim it was judged as one.
                "intent": "UNCLASSIFIED" if intent is None else intent.value,
                "instrument": str(order.key),
                "side": order.side.value,
                "product": order.product.value,
                "quantity": order.quantity,
                **extra,
            },
        )  # fmt: skip

    def _rule_event(self, order: Order, result: RuleResult, now: datetime) -> RiskEvent:
        return RiskEvent(
            at=now, account_id=self._account_id, rule=result.rule, phase=result.phase.value,
            decision=result.decision.severity.name, enforced=result.enforced,
            latency_us=result.latency_us, reason_code=result.decision.code,
            inputs={"order_id": order.order_id, **result.decision.inputs},
        )  # fmt: skip


def _verdict_severity(reason: str | None, results: list[RuleResult]) -> Severity:
    """ALLOW, or how severe the deciding rule was (REJECT for a gate-level refusal)."""
    if reason is None:
        return Severity.ALLOW
    winner = most_severe(results)
    return winner.effective if winner is not None else Severity.REJECT
