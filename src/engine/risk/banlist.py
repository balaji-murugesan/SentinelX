"""Holds the exchange's F&O ban lists in memory and applies the ``SetBanList`` command to them
(add-risk-guards task 7.2; instrument-policy spec; design §7).

Trading terms:
  * F&O ban list - the underlyings the exchange bars from *new* futures and options positions for
                   one trading day. NSE (and BSE for its own F&O) publishes it each evening for the
                   next day. A name enters it when the open interest across all its futures and
                   options passes 95% of its MWPL, and leaves once that falls below 80%. While
                   banned, traders may only reduce positions in it; a new position draws an
                   exchange penalty.
  * MWPL         - market-wide position limit: the most open interest the exchange allows in one
                   underlying's F&O contracts, across every trader.
  * scope        - the ban covers the equity-derivative segments only, ``NSE_FNO`` and ``BSE_FNO``
                   (owner ruling 2026-11-21). Commodity (``MCX_COMM``) and currency contracts have
                   no such list, so they are never refused for it. The segment set lives here, in
                   ``BAN_LIST_SEGMENTS``; it is deliberately not the lot caps' exemption set.

``BanListService`` is the one writer of the stored lists. The rest of the engine reads
``for_date`` (the gate and the monitor, through ``RiskContext.ban_list``), so a check never touches
the database.

  * **Load.** ``load`` reads today's list and any loaded ahead for later dates. It is called at
    start and retried by the monitor on every pass while the lists are unread. Until a read
    succeeds, ``for_date`` answers None for every date, which the instrument policy treats as "no
    list": F&O entries are refused (fail closed). Exits are never judged by it.
  * **Command.** ``set_ban_list`` stores one day's list. A date before today is refused. During
    market hours today's list may only grow: a list that leaves out a symbol already loaded for
    today is refused with ``TIGHTEN_ONLY`` (the exchange can add a name during the day but a
    removal waits for the next list). Outside market hours a list replaces the stored one. An
    empty list is accepted and means "no bans that day", which is different from no list.
  * **Audit.** Every accepted or refused command appends one risk record (rule ``ban_list``) with
    the date, the symbols and the outcome.

Idempotency and the lock follow ``LimitsService``: one ``asyncio.Lock`` (a C# ``SemaphoreSlim(1)``)
around read, check and save, because the store has no compare-and-set; and the command-result
table keyed by idempotency key, so a repeated key returns the first answer and changes nothing.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Final

import structlog

from contracts.commands import CommandResult, SetBanList
from contracts.market import ExchangeSegment
from engine.market_data.clock import is_market_hours, ist_date, utc_now
from engine.persistence.repository import PersistenceError, TradingRepository
from engine.risk.limits import STATE_UNAVAILABLE, TIGHTEN_ONLY
from engine.risk.repository import AuditValue, RiskEvent, RiskRepository

log = structlog.get_logger("engine.risk.banlist")

RULE: Final = "ban_list"  # the rule id on audit records of ban-list loads
PHASE: Final = "config"
BAN_LIST_INVALID: Final = "RISK_BAN_LIST_INVALID"  # a past date, a blank symbol, another account

# The segments the exchange's F&O ban list covers (instrument-policy spec "Entries in banned
# underlyings are refused"). An allow-list on purpose, unlike the lot caps' deny-list: the ban is
# an exchange rule for equity derivatives only, so a segment the exchange does not ban in
# (commodity, currency, one added later) must not be refused for a missing list.
BAN_LIST_SEGMENTS: Final = frozenset({ExchangeSegment.NSE_FNO, ExchangeSegment.BSE_FNO})


def normalise(symbols: list[str]) -> frozenset[str]:
    """Upper-cased, trimmed symbols. Raises ``ValueError`` for a blank one."""
    cleaned = [s.strip().upper() for s in symbols]
    if any(not s for s in cleaned):
        raise ValueError("a symbol is blank")
    return frozenset(cleaned)


class BanListService:
    def __init__(
        self,
        *,
        account_id: str,
        repository: RiskRepository,
        results: TradingRepository,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._account_id = account_id
        self._repo = repository
        self._results = results
        self._now = now
        self._lock = asyncio.Lock()
        # The stored lists for today and later dates, or None until a read has succeeded.
        self._lists: dict[date, frozenset[str]] | None = None

    @property
    def read(self) -> bool:
        """Has a read of the stored lists succeeded (so ``for_date`` can be trusted)?"""
        return self._lists is not None

    def for_date(self, day: date) -> frozenset[str] | None:
        """The ban list for ``day`` (IST), or None: none is loaded for it, or the stored lists
        have not been read. None refuses every F&O entry."""
        return None if self._lists is None else self._lists.get(day)

    def for_now(self, now: datetime) -> frozenset[str] | None:
        """Today's list, for the IST date of ``now``."""
        return self.for_date(ist_date(now))

    async def load(self) -> bool:
        """Read today's list and the ones loaded ahead. True once the storage answered; False if
        it did not (the lists stay unread and F&O entries refused; the monitor retries)."""
        async with self._lock:
            return await self._load()

    async def _load(self) -> bool:
        today = ist_date(self._now())
        try:
            stored = await self._repo.load_ban_list(self._account_id, today)
        except PersistenceError as error:
            log.error("ban_list_not_loaded", error=str(error))
            return False
        self._lists = {s.trade_date: s.symbols for s in stored}
        log.info("ban_list_read", dates=[d.isoformat() for d in sorted(self._lists)])
        return True

    # ---- the SetBanList command ------------------------------------------------------------

    async def set_ban_list(self, command: SetBanList) -> CommandResult:
        """Apply one ban-list command completely or not at all, and answer it."""
        if command.account_id != self._account_id:
            # Refused, and recorded like every other refused load (instrument-policy spec).
            return await self._refused(
                command, BAN_LIST_INVALID,
                f"command is for account {command.account_id!r}, not {self._account_id!r}",
                time.perf_counter(),
            )  # fmt: skip
        async with self._lock:
            try:
                first = await self._results.find_result(command.account_id, command.idempotency_key)
            except PersistenceError as error:
                return self._answer(command, f"{STATE_UNAVAILABLE}: {error}")
            if first is not None:
                return first  # the same key twice: the first answer, nothing else happens
            result = await self._apply(command)
            if (result.reason or "").startswith(STATE_UNAVAILABLE):
                # A storage failure is not an answer to remember: the same key may succeed later.
                return result
            try:
                await self._results.save_result(result)
            except PersistenceError as error:
                # The load stands; a repeat of the key is judged again and changes nothing.
                log.warning("ban_list_result_not_stored", error=str(error))
            return result

    async def _apply(self, command: SetBanList) -> CommandResult:
        started = time.perf_counter()
        at = self._now()
        today = ist_date(at)
        try:
            symbols = normalise(command.symbols)
        except ValueError as error:
            return await self._refused(command, BAN_LIST_INVALID, str(error), started)
        if command.trade_date < today:
            return await self._refused(
                command, BAN_LIST_INVALID,
                f"{command.trade_date.isoformat()} is before today ({today.isoformat()})",
                started,
            )  # fmt: skip
        # Read the store again (it is the truth, and a successful read also ends an "unread"
        # state), so the tighten-only check compares with what is stored, not a stale copy.
        if not await self._load():
            return await self._refused(
                command, STATE_UNAVAILABLE, "the stored ban lists cannot be read", started
            )
        assert self._lists is not None  # set by the successful read just above
        current = self._lists.get(command.trade_date)
        if command.trade_date == today and is_market_hours(at) and current is not None:
            missing = current - symbols
            if missing:
                return await self._refused(
                    command, TIGHTEN_ONLY,
                    "during market hours today's ban list may only grow; it would drop "
                    + ", ".join(sorted(missing)),
                    started,
                )  # fmt: skip
        try:
            stored = await self._repo.save_ban_list(
                self._account_id, command.trade_date, symbols, at
            )
        except PersistenceError as error:
            # The error can arrive after the write committed. Read the store back and believe it;
            # if it cannot be read, forget the lists (F&O entries refused) rather than keep a copy
            # that may hold fewer bans than the store.
            if not await self._load():
                self._lists = None
            if self.for_date(command.trade_date) != symbols:
                return await self._refused(command, STATE_UNAVAILABLE, str(error), started)
        else:
            self._lists[stored.trade_date] = stored.symbols
        await self._audit(command, symbols, "ALLOW", None, None, started, at)
        return self._answer(command, None)

    async def _refused(
        self, command: SetBanList, code: str, problem: str, started: float
    ) -> CommandResult:
        try:
            symbols: frozenset[str] = normalise(command.symbols)
        except ValueError:
            symbols = frozenset(command.symbols)
        await self._audit(command, symbols, "REJECT", code, problem, started, self._now())
        return self._answer(command, f"{code}: {problem}")

    async def _audit(
        self,
        command: SetBanList,
        symbols: frozenset[str],
        decision: str,
        code: str | None,
        problem: str | None,
        started: float,
        at: datetime,
    ) -> None:
        """Append the load's record. A record that cannot be written is logged with what it said,
        never raised: the load's outcome does not depend on its audit row."""
        inputs: dict[str, AuditValue] = {
            "outcome": "accepted" if decision == "ALLOW" else "rejected",
            "trade_date": command.trade_date.isoformat(),
            "symbols": ",".join(sorted(symbols)),
            "count": len(symbols),
            "source": command.source,
            "idempotency_key": command.idempotency_key,
            "problem": problem,
        }
        try:
            await self._repo.append_event(
                RiskEvent(
                    at=at,
                    account_id=self._account_id,
                    rule=RULE,
                    phase=PHASE,
                    decision=decision,
                    enforced=True,
                    latency_us=int((time.perf_counter() - started) * 1_000_000),
                    reason_code=code,
                    inputs=inputs,
                )  # fmt: skip
            )
        except Exception as error:
            log.error("ban_list_audit_not_stored", **inputs, reason_code=code, error=str(error))

    def _answer(self, command: SetBanList, reason: str | None) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            account_id=self._account_id,
            accepted=reason is None,
            reason=reason,
            decided_at=self._now(),
        )
