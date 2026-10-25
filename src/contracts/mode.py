"""The paper/live gate: decides whether this process may send real orders.

Pure logic, no I/O and no logging, so it is trivially testable and `contracts` stays
dependency-free (it must not import other internal packages).

Safety rules enforced here (CLAUDE.md "Safety rules" #1, PRD §9.3):
  * Paper is the default.
  * Live needs BOTH ``TRADING_MODE=live`` and ``LIVE_CONFIRM=yes``, matched exactly.
  * Anything unrecognised is an error. There is deliberately no silent fallback to paper:
    that would hide a misconfiguration in production.
"""

from enum import StrEnum

LIVE_CONFIRM_VALUE = "yes"


class TradingMode(StrEnum):
    """StrEnum is like a C# enum whose members are also strings ("paper", "live")."""

    PAPER = "paper"
    LIVE = "live"


class ModeConfigError(ValueError):
    """Raised when TRADING_MODE / LIVE_CONFIRM do not describe a valid mode.

    Subclasses ValueError so pydantic turns it into a normal validation error.
    """


def resolve_trading_mode(trading_mode: str | None, live_confirm: str | None) -> TradingMode:
    """Return the effective mode, or raise ModeConfigError. Never guesses.

    ``None`` (variable unset) and ``""`` (set but empty) are treated the same.
    Comparison is exact: case and surrounding whitespace matter.
    """
    confirm = live_confirm or ""
    mode = trading_mode or ""

    # Rule 1: a malformed confirmation is refused in every mode. Someone who typed
    # LIVE_CONFIRM=true probably meant to go live; stopping beats guessing.
    if confirm not in ("", LIVE_CONFIRM_VALUE):
        raise ModeConfigError(
            f"LIVE_CONFIRM must be exactly {LIVE_CONFIRM_VALUE!r} or unset, got {confirm!r}"
        )

    # Rule 2/3: unset, empty or "paper" -> paper (a stray LIVE_CONFIRM=yes is harmless here).
    if mode in ("", TradingMode.PAPER.value):
        return TradingMode.PAPER

    # Rule 4: live needs the explicit confirmation.
    if mode == TradingMode.LIVE.value:
        if confirm != LIVE_CONFIRM_VALUE:
            raise ModeConfigError(
                "TRADING_MODE=live requires LIVE_CONFIRM=yes (exact value); "
                "refusing to start in live mode without it"
            )
        return TradingMode.LIVE

    # Rule 5: anything else is an error, not paper.
    allowed = f"{TradingMode.PAPER.value!r} or {TradingMode.LIVE.value!r}"
    hint = (
        " (values are case-sensitive, with no surrounding whitespace)"
        if mode.strip().lower() in (m.value for m in TradingMode)
        else ""
    )
    raise ModeConfigError(f"TRADING_MODE must be {allowed} (or unset), got {mode!r}{hint}")


def startup_banner(service: str, mode: TradingMode) -> str:
    """One-line, hard-to-miss banner naming the service and its trading mode."""
    if mode is TradingMode.LIVE:
        return (
            f"=== SENTINELX {service.upper()} | TRADING MODE: LIVE | REAL ORDERS WILL BE SENT ==="
        )
    return (
        f"=== SENTINELX {service.upper()} | TRADING MODE: PAPER | "
        "simulated fills, no broker orders ==="
    )
