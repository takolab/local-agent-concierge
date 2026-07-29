import logging
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from slack_gateway.config import Settings


def create_slack_app(settings: Settings) -> App:
    app = App(token=settings.slack_bot_token)

    @app.event("message")
    def handle_message(
        event: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        if event.get("bot_id") or event.get("subtype"):
            return

        logger.info(
            "Slack message received "
            "(channel=%s user=%s ts=%s thread_ts=%s)",
            event.get("channel"),
            event.get("user"),
            event.get("ts"),
            event.get("thread_ts"),
        )

    return app


def run_socket_mode(settings: Settings) -> None:
    app = create_slack_app(settings)

    handler = SocketModeHandler(
        app,
        settings.slack_app_token,
    )
    handler.start()
