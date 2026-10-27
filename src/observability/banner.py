"""Logs the startup banner that tells the operator which trading mode a service is in."""

import structlog

from contracts.mode import TradingMode, startup_banner


def log_startup_banner(service: str, mode: TradingMode, feed: str | None = None) -> None:
    """Emit one banner record. Live mode is logged at WARNING so it stands out.

    Only the service name, mode and feed source are passed on, never the settings object, so
    no secret can end up in this record.
    """
    logger = structlog.get_logger(service)
    banner = startup_banner(service, mode, feed)
    fields = {"service": service, "mode": mode.value, **({"feed": feed} if feed else {})}
    if mode is TradingMode.LIVE:
        logger.warning(banner, **fields)
    else:
        logger.info(banner, **fields)
