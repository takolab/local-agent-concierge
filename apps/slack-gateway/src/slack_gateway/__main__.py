import logging

from slack_gateway.config import load_settings
from slack_gateway.slack_app import run_socket_mode
from slack_gateway.telemetry import configure_tracing


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    configure_tracing()

    logger = logging.getLogger("slack_gateway")
    settings = load_settings()

    logger.info(
        "Starting Slack Gateway "
        "(Hermes API: %s)",
        settings.hermes_api_base_url,
    )

    run_socket_mode(settings)


if __name__ == "__main__":
    main()
