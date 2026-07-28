import logging
from threading import Event


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger = logging.getLogger("slack_gateway")
    logger.info("Slack Gateway initialized")

    Event().wait()


if __name__ == "__main__":
    main()
