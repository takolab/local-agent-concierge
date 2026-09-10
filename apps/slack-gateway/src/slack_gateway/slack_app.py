import logging
from typing import Any

from agent_contracts.agent_request import AgentRequest
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

from slack_gateway.config import Settings
from slack_gateway.event_deduplicator import EventDeduplicator
from slack_gateway.orchestrator_client import (
    ERROR_TYPE_DISPATCH_FAILED,
    ERROR_TYPE_OUTCOME_UNKNOWN,
    HERMES_AGENT_NAME,
    OrchestratorClient,
    dispatch_error_type,
)
from slack_gateway.telemetry import (
    mark_span_error,
    trace_orchestrator_request,
    trace_slack_request,
    trace_slack_response,
)


PROCESSING_MESSAGE = ":hourglass_flowing_sand: Working on it…"

ERROR_MESSAGE = (
    ":warning: I couldn't complete that request. "
    "Please try again."
)

# Shown when the dispatch's outcome is unknown rather than failed -- the
# request may have been delivered and may still be running, or the Agent
# ran and only its result was lost. Deliberately does NOT invite a retry:
# the Agent reachable through this path is tool-capable (Hermes Agent runs
# its configured toolsets and MCP servers), so an immediate retry can
# duplicate a real side effect. See
# docs/slack-gateway/orchestrator-dispatch.md.
UNKNOWN_OUTCOME_MESSAGE = (
    ":warning: I lost contact while the request was being processed. "
    "The result is unknown, so please check before retrying."
)

# The user-facing text for each classification `dispatch_error_type`
# produces. Keyed by that vocabulary rather than by exception type so
# there is exactly one place deciding what a failure *is*, and this map
# only decides how to say it.
FAILURE_MESSAGES = {
    ERROR_TYPE_DISPATCH_FAILED: ERROR_MESSAGE,
    ERROR_TYPE_OUTCOME_UNKNOWN: UNKNOWN_OUTCOME_MESSAGE,
}

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

def handle_slack_message(
    *,
    event: dict[str, Any],
    body: dict[str, Any],
    client: WebClient,
    logger: logging.Logger,
    orchestrator_client: OrchestratorClient,
    event_deduplicator: EventDeduplicator,
) -> None:
    """Process one Slack `message` event end to end.

    A module-level function rather than a closure inside
    `create_slack_app` so the routing path it owns -- Slack event ->
    `AgentRequest` -> Orchestrator `POST /dispatch` -> Slack reply -- is
    directly testable without constructing a `slack_bolt.App` (which
    would need real Slack credentials). `create_slack_app` registers a
    thin adapter that forwards Bolt's injected arguments here.
    """
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

    # `user` joined this check when dispatch moved to the Orchestrator:
    # `AgentRequest.user_id` is a required non-empty string, and the
    # honest response to a message whose author Slack did not identify is
    # to ignore it -- not to invent a placeholder identity for a field
    # whose whole purpose is correlation. A non-bot, non-subtype message
    # event always carries `user`, so this is a guard, not a new path
    # real traffic is expected to take.
    if not all(
        isinstance(value, str) and value
        for value in (
            workspace_id,
            channel_id,
            user_id,
            message_ts,
        )
    ):
        logger.warning(
            "Ignoring Slack message with incomplete routing data "
            "(event_id=%s workspace=%s channel=%s user=%s ts=%s)",
            event_id,
            workspace_id,
            channel_id,
            user_id,
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

    # Every field is already validated above, so this cannot raise:
    # `event_id`, `user_id` and `conversation` are non-empty strings and
    # `text.strip()` is non-blank. `trace_id` stays `None` on purpose --
    # see docs/slack-gateway/orchestrator-dispatch.md: it is an
    # application-level correlation field, not the trace-propagation
    # mechanism (that is the `traceparent` header, injected by
    # `OrchestratorClient`), and what a caller should put in it is still
    # an open schema question. `permissions` stays empty because nothing
    # in this path authorizes anything.
    agent_request = AgentRequest(
        task_id=event_id,
        user_id=user_id,
        conversation_id=conversation,
        instruction=text.strip(),
        memory_scopes=(),
        permissions=(),
        trace_id=None,
    )

    with trace_slack_request(
        threaded=thread_ts is not None,
    ) as request_span:
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
            "Dispatching Slack message to the Orchestrator "
            "(event_id=%s channel=%s user=%s ts=%s "
            "agent=%s conversation=%s processing_ts=%s)",
            event_id,
            channel_id,
            user_id,
            message_ts,
            HERMES_AGENT_NAME,
            conversation,
            processing_message_ts,
        )

        # The Agent is selected here, by this service, as a fixed name.
        # The Orchestrator dispatches through its registered Agent
        # boundary but performs no classification or selection, so this
        # constant -- not the Orchestrator -- is what decides which Agent
        # runs a Slack request today.
        try:
            with trace_orchestrator_request():
                agent_response = orchestrator_client.dispatch(
                    HERMES_AGENT_NAME,
                    agent_request,
                )
        except RuntimeError as error:
            # Two materially different outcomes, told apart by the
            # exception's type: the dispatch definitely did not run, or it
            # may have run and the outcome is unknown. Anything this
            # client did not classify as a definite failure lands in the
            # unknown bucket -- see `dispatch_error_type`.
            error_type = dispatch_error_type(error)
            failure_text = FAILURE_MESSAGES[error_type]

            mark_span_error(
                request_span,
                error_type=error_type,
            )

            logger.exception(
                "Failed to process Slack message through the "
                "Orchestrator "
                "(event_id=%s channel=%s user=%s ts=%s agent=%s "
                "outcome=%s)",
                event_id,
                channel_id,
                user_id,
                message_ts,
                HERMES_AGENT_NAME,
                error_type,
            )

            try:
                with trace_slack_response():
                    delivery_method = _post_thread_message_and_remove_processing_status(
                        client=client,
                        channel_id=channel_id,
                        thread_ts=root_thread_ts,
                        processing_message_ts=processing_message_ts,
                        text=failure_text,
                        logger=logger,
                        event_id=event_id,
                    )
            except SlackApiError:
                logger.exception(
                    "Failed to display the Orchestrator processing "
                    "error in Slack "
                    "(event_id=%s channel=%s ts=%s "
                    "thread_ts=%s)",
                    event_id,
                    channel_id,
                    message_ts,
                    root_thread_ts,
                )
                return

            logger.info(
                "Orchestrator processing error displayed in Slack "
                "(event_id=%s channel=%s ts=%s "
                "thread_ts=%s outcome=%s delivery=%s)",
                event_id,
                channel_id,
                message_ts,
                root_thread_ts,
                error_type,
                delivery_method,
            )
            return

        # `AgentResponse.status` is recorded but deliberately not branched
        # on. No status vocabulary is defined anywhere in this repository
        # yet (docs/agent-contracts/domain-model.md, open question), and
        # `Orchestrator.dispatch()` itself returns whatever status the
        # Agent produced without inspecting it. Inventing a mapping here
        # would be this service guessing at a contract that does not
        # exist. Delivering the summary matches the behavior this path had
        # before the rewiring, when any successful Hermes response was
        # posted as-is.
        response_text = agent_response.summary

        logger.info(
            "Agent response received "
            "(event_id=%s channel=%s ts=%s agent=%s "
            "status=%s response_chars=%d)",
            event_id,
            channel_id,
            message_ts,
            HERMES_AGENT_NAME,
            agent_response.status,
            len(response_text),
        )

        try:
            with trace_slack_response():
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
            mark_span_error(
                request_span,
                error_type="slack.response_error",
            )

            logger.exception(
                "Failed to deliver the agent response to Slack "
                "(event_id=%s channel=%s ts=%s thread_ts=%s)",
                event_id,
                channel_id,
                message_ts,
                root_thread_ts,
            )
            return

        logger.info(
            "Agent response delivered to Slack "
            "(event_id=%s channel=%s ts=%s "
            "thread_ts=%s delivery=%s)",
            event_id,
            channel_id,
            message_ts,
            root_thread_ts,
            delivery_method,
        )

def create_slack_app(
    settings: Settings,
    orchestrator_client: OrchestratorClient,
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
        handle_slack_message(
            event=event,
            body=body,
            client=client,
            logger=logger,
            orchestrator_client=orchestrator_client,
            event_deduplicator=event_deduplicator,
        )

    return app


def run_socket_mode(settings: Settings) -> None:
    orchestrator_client = OrchestratorClient(
        base_url=settings.orchestrator_base_url,
    )
    event_deduplicator = EventDeduplicator()

    app = create_slack_app(
        settings=settings,
        orchestrator_client=orchestrator_client,
        event_deduplicator=event_deduplicator,
    )

    handler = SocketModeHandler(
        app,
        settings.slack_app_token,
    )

    try:
        handler.start()
    finally:
        orchestrator_client.close()
