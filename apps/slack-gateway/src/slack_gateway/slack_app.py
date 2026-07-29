import logging
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from slack_gateway.config import Settings
from slack_gateway.hermes_client import HermesClient


def create_slack_app(
    settings: Settings,
    hermes_client: HermesClient,
) -> App:
    app = App(token=settings.slack_bot_token)

    @app.event("message")
    def handle_message(
        event: dict[str, Any],
        body: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        # Ignore messages posted by bots and events with subtypes,
        # such as message edits.
        if event.get("bot_id") or event.get("subtype"):
            return

        text = event.get("text")
        workspace_id = body.get("team_id")
        channel_id = event.get("channel")
        user_id = event.get("user")
        message_ts = event.get("ts")

        if not isinstance(text, str) or not text.strip():
            logger.info(
                "Ignoring Slack message without text "
                "(channel=%s user=%s ts=%s)",
                channel_id,
                user_id,
                message_ts,
            )
            return

        if not all(
            isinstance(value, str) and value
            for value in (
                workspace_id,
                channel_id,
                message_ts,
            )
        ):
            logger.warning(
                "Ignoring Slack message with incomplete routing data "
                "(workspace=%s channel=%s ts=%s)",
                workspace_id,
                channel_id,
                message_ts,
            )
            return

        # Use the parent message timestamp for thread replies.
        # For a top-level DM, use the message timestamp as the thread root.
        root_thread_ts = event.get("thread_ts") or message_ts

        conversation = (
            f"slack:{workspace_id}:{channel_id}:{root_thread_ts}"
        )

        logger.info(
            "Forwarding Slack message to Hermes "
            "(channel=%s user=%s ts=%s conversation=%s)",
            channel_id,
            user_id,
            message_ts,
            conversation,
        )

        try:
            response_text = hermes_client.create_response(
                input_text=text.strip(),
                conversation=conversation,
            )
        except RuntimeError:
            logger.exception(
                "Failed to process Slack message with Hermes "
                "(channel=%s user=%s ts=%s)",
                channel_id,
                user_id,
                message_ts,
            )
            return

        logger.info(
            "Hermes response received "
            "(channel=%s ts=%s response_chars=%d)",
            channel_id,
            message_ts,
            len(response_text),
        )

    return app


def run_socket_mode(settings: Settings) -> None:
    hermes_client = HermesClient(
        base_url=settings.hermes_api_base_url,
        api_key=settings.hermes_api_server_key,
    )

    app = create_slack_app(
        settings=settings,
        hermes_client=hermes_client,
    )

    handler = SocketModeHandler(
        app,
        settings.slack_app_token,
    )

    try:
        handler.start()
    finally:
        hermes_client.close()
