import logging
from threading import Event

from slack_gateway.config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger = logging.getLogger("slack_gateway")
    settings = load_settings()

    logger.info(
        "Slack Gateway configuration loaded (Hermes API: %s)",
        settings.hermes_api_base_url,
    )

    Event().wait()


if __name__ == "__main__":
    main()
