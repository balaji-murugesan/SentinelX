"""Api entry point. The FastAPI app arrives in task 4.1; for now it validates config and
announces its mode."""

from api.config import ApiSettings
from contracts.settings import SettingsError, load_settings
from observability.banner import log_startup_banner
from observability.logging_setup import configure_logging
from observability.tracing import configure_tracing


def main() -> None:
    try:
        settings = load_settings(ApiSettings)
    except SettingsError as exc:
        raise SystemExit(str(exc)) from None
    configure_logging("api", settings.log_level)
    configure_tracing("api", settings.otel_exporter_otlp_endpoint)
    log_startup_banner("api", settings.mode)


if __name__ == "__main__":
    main()
