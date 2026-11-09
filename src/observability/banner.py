"""Logs the startup banner that tells the operator which trading mode a service is in."""

import structlog

from contracts.mode import TradingMode, startup_banner


def log_startup_banner(
    service: str, mode: TradingMode, feed: str | None = None, account: str | None = None
) -> None:
    """Emit one banner record. Live mode is logged at WARNING so it stands out.

    Only the service name, mode and feed source are passed on, never the settings object, so
    no secret can end up in this record.
    """
    logger = structlog.get_logger(service)
    banner = startup_banner(service, mode, feed, account)
    fields = {
        "service": service,
        "mode": mode.value,
        **({"feed": feed} if feed else {}),
        **({"account": account} if account else {}),
    }
    if mode is TradingMode.LIVE:
        logger.warning(banner, **fields)
    else:
        logger.info(banner, **fields)


def log_halted_banner(service: str, account: str, differences: list[str]) -> None:
    """Emit the HALTED block at ERROR level: one banner line, then one record per difference.

    A halted engine refuses new orders until the owner reviews and runs ``cli recovery resume``
    (add-order-position-core design §9). Differences are ids and numbers only, never secrets.
    """
    logger = structlog.get_logger(service)
    logger.error(
        f"=== SENTINELX {service.upper()} | HALTED | NEW ORDERS REFUSED | "
        f"ACCOUNT: {account.upper()} | {len(differences)} DIFFERENCE(S) ===",
        service=service,
        account=account,
        halted=True,
    )
    for difference in differences:
        logger.error(f"HALTED: {difference}", service=service, account=account)
    logger.error(
        "HALTED: review the above, fix the cause, then run `python -m cli recovery resume`"
    )
