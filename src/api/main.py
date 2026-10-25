"""Api entry point (``uv run --env-file .env python -m api.main``)."""

import uvicorn

from api.app import create_app
from api.config import ApiSettings
from contracts.settings import SettingsError, load_settings
from observability.banner import log_startup_banner
from observability.logging_setup import configure_logging
from observability.tracing import configure_tracing, shutdown_tracing

HOST = "0.0.0.0"  # noqa: S104 (inside the container; Compose publishes nothing but Caddy)
PORT = 8000


def start() -> ApiSettings:
    """Load and validate configuration, set up logging and tracing, announce the mode.

    Refuses to start (exits with the error message) on invalid configuration.
    """
    try:
        settings = load_settings(ApiSettings)
    except SettingsError as exc:
        raise SystemExit(str(exc)) from None
    configure_logging("api", settings.log_level)
    configure_tracing("api", settings.otel_exporter_otlp_endpoint)
    log_startup_banner("api", settings.mode)
    return settings


def main() -> None:
    settings = start()
    try:
        # log_config=None keeps uvicorn from installing its own plain-text logging; the access
        # log is off because every request already becomes a trace span.
        uvicorn.run(create_app(settings), host=HOST, port=PORT, log_config=None, access_log=False)
    finally:
        shutdown_tracing()  # flush any pending spans before exiting


if __name__ == "__main__":
    main()
