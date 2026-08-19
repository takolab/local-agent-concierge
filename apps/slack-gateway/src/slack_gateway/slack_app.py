import logging
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

from slack_gateway.config import Settings
from slack_gateway.event_deduplicator import EventDeduplicator
from slack_gateway.hermes_client import HermesClient
from slack_gateway.telemetry import (
    trace_hermes_request,
    trace_slack_request,
)


PROCESSING_MESSAGE = ":hourglass_flowing_sand: Working on it…"

ERROR_MESSAGE = (
    ":warning: I couldn't complete that request. "
    "Please try again."
)

def _post_thread_message_and_remove_processing_status(
    *,
    client: WebClient,
    channel_id: str,
    thread_ts: str,
    processing_message_ts: str | None,
    text: str,
    logger: logging.Logger,
    event_id: str,
) -> str:
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=text,
    )

    if processing_message_ts is None:
        return "posted"

    try:
        client.chat_delete(
            channel=channel_id,
            ts=processing_message_ts,
        )
    except SlackApiError:
        logger.exception(
            "Failed to delete processing status after posting "
            "the final message "
            "(event_id=%s channel=%s thread_ts=%s "
            "processing_ts=%s)",
            event_id,
            channel_id,
            thread_ts,
            processing_message_ts,
        )
        return "posted_with_processing_status_remaining"

    return "posted_and_processing_status_deleted"

def create_slack_app(
    settings: Settings,
    hermes_client: HermesClient,
    event_deduplicator: EventDeduplicator,
) -> App:
    app = App(token=settings.slack_bot_token)

    @app.event("message")
    def handle_message(
        event: dict[str, Any],
        body: dict[str, Any],
        client: WebClient,
        logger: logging.Logger,
    ) -> None:
        # Ignore messages posted by bots and events with subtypes,
        # such as message edits.
        if event.get("bot_id") or event.get("subtype"):
            return

        event_id = body.get("event_id")
        text = event.get("text")
        workspace_id = body.get("team_id")
        channel_id = event.get("channel")
        user_id = event.get("user")
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        if not isinstance(event_id, str) or not event_id:
            logger.warning(
                "Ignoring Slack message without a valid event ID "
                "(channel=%s user=%s ts=%s)",
                channel_id,
                user_id,
                message_ts,
            )
            return

        if not isinstance(text, str) or not text.strip():
            logger.info(
                "Ignoring Slack message without text "
                "(event_id=%s channel=%s user=%s ts=%s)",
                event_id,
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
                "(event_id=%s workspace=%s channel=%s ts=%s)",
                event_id,
                workspace_id,
                channel_id,
                message_ts,
            )
            return

        if thread_ts is not None and (
            not isinstance(thread_ts, str) or not thread_ts
        ):
            logger.warning(
                "Ignoring Slack message with invalid thread timestamp "
                "(event_id=%s channel=%s user=%s ts=%s thread_ts=%s)",
                event_id,
                channel_id,
                user_id,
                message_ts,
                thread_ts,
            )
            return

        if not event_deduplicator.claim(event_id):
            logger.info(
                "Ignoring duplicate Slack event "
                "(event_id=%s channel=%s ts=%s)",
                event_id,
                channel_id,
                message_ts,
            )
            return

        # Use the parent message timestamp for thread replies.
        # For a top-level DM, use the message timestamp as the thread root.
        root_thread_ts = thread_ts or message_ts

        conversation = (
            f"slack:{workspace_id}:{channel_id}:{root_thread_ts}"
        )

        with trace_slack_request(
            threaded=thread_ts is not None,
        ):
            processing_message_ts: str | None = None

            try:
                processing_response = client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=root_thread_ts,
                    text=PROCESSING_MESSAGE,
                )
            except SlackApiError:
                logger.exception(
                    "Failed to post processing status to Slack "
                    "(event_id=%s channel=%s ts=%s thread_ts=%s)",
                    event_id,
                    channel_id,
                    message_ts,
                    root_thread_ts,
                )
            else:
                returned_ts = processing_response.get("ts")

                if isinstance(returned_ts, str) and returned_ts:
                    processing_message_ts = returned_ts
                else:
                    logger.warning(
                        "Processing status response did not contain "
                        "a valid timestamp "
                        "(event_id=%s channel=%s ts=%s)",
                        event_id,
                        channel_id,
                        message_ts,
                    )

            logger.info(
                "Forwarding Slack message to Hermes "
                "(event_id=%s channel=%s user=%s ts=%s "
                "conversation=%s processing_ts=%s)",
                event_id,
                channel_id,
                user_id,
                message_ts,
                conversation,
                processing_message_ts,
            )

            try:
                with trace_hermes_request():
                    response_text = hermes_client.create_response(
                        input_text=text.strip(),
                        conversation=conversation,
                    )
            except RuntimeError:
                logger.exception(
                    "Failed to process Slack message with Hermes "
                    "(event_id=%s channel=%s user=%s ts=%s)",
                    event_id,
                    channel_id,
                    user_id,
                    message_ts,
                )

                try:
                    delivery_method = _post_thread_message_and_remove_processing_status(
                        client=client,
                        channel_id=channel_id,
                        thread_ts=root_thread_ts,
                        processing_message_ts=processing_message_ts,
                        text=ERROR_MESSAGE,
                        logger=logger,
                        event_id=event_id,
                    )
                except SlackApiError:
                    logger.exception(
                        "Failed to display Hermes processing error "
                        "in Slack "
                        "(event_id=%s channel=%s ts=%s "
                        "thread_ts=%s)",
                        event_id,
                        channel_id,
                        message_ts,
                        root_thread_ts,
                    )
                    return

                logger.info(
                    "Hermes processing error displayed in Slack "
                    "(event_id=%s channel=%s ts=%s "
                    "thread_ts=%s delivery=%s)",
                    event_id,
                    channel_id,
                    message_ts,
                    root_thread_ts,
                    delivery_method,
                )
                return

            logger.info(
                "Hermes response received "
                "(event_id=%s channel=%s ts=%s response_chars=%d)",
                event_id,
                channel_id,
                message_ts,
                len(response_text),
            )

            try:
                delivery_method = _post_thread_message_and_remove_processing_status(
                    client=client,
                    channel_id=channel_id,
                    thread_ts=root_thread_ts,
                    processing_message_ts=processing_message_ts,
                    text=response_text,
                    logger=logger,
                    event_id=event_id,
                )
            except SlackApiError:
                logger.exception(
                    "Failed to deliver Hermes response to Slack "
                    "(event_id=%s channel=%s ts=%s thread_ts=%s)",
                    event_id,
                    channel_id,
                    message_ts,
                    root_thread_ts,
                )
                return

            logger.info(
                "Hermes response delivered to Slack "
                "(event_id=%s channel=%s ts=%s "
                "thread_ts=%s delivery=%s)",
                event_id,
                channel_id,
                message_ts,
                root_thread_ts,
                delivery_method,
            )

    return app


def run_socket_mode(settings: Settings) -> None:
    hermes_client = HermesClient(
        base_url=settings.hermes_api_base_url,
        api_key=settings.hermes_api_server_key,
    )
    event_deduplicator = EventDeduplicator()

    app = create_slack_app(
        settings=settings,
        hermes_client=hermes_client,
        event_deduplicator=event_deduplicator,
    )

    handler = SocketModeHandler(
        app,
        settings.slack_app_token,
    )

    try:
        handler.start()
    finally:
        hermes_client.close()