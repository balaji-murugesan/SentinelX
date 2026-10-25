"""Logs the startup banner that tells the operator which trading mode a service is in."""

import structlog

from contracts.mode import TradingMode, startup_banner


def log_startup_banner(service: str, mode: TradingMode) -> None:
    """Emit one banner record. Live mode is logged at WARNING so it stands out.

    Only the service name and mode are passed on, never the settings object, so no secret
    can end up in this record.
    """
    logger = structlog.get_logger(service)
    banner = startup_banner(service, mode)
    if mode is TradingMode.LIVE:
        logger.warning(banner, service=service, mode=mode.value)
    else:
        logger.info(banner, service=service, mode=mode.value)
