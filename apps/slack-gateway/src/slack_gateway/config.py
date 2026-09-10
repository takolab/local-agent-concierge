import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    orchestrator_base_url: str


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(f"{name} is required")

    return value


def load_settings() -> Settings:
    slack_bot_token = _required_env("SLACK_BOT_TOKEN")
    slack_app_token = _required_env("SLACK_APP_TOKEN")

    if not slack_bot_token.startswith("xoxb-"):
        raise RuntimeError("SLACK_BOT_TOKEN must start with 'xoxb-'")

    if not slack_app_token.startswith("xapp-"):
        raise RuntimeError("SLACK_APP_TOKEN must start with 'xapp-'")

    # The Orchestrator is the Slack Gateway's single dispatch authority.
    # HERMES_API_BASE_URL / HERMES_API_SERVER_KEY are deliberately no
    # longer read here: the Gateway no longer calls Hermes Agent, and the
    # Hermes credential now lives only in the service that owns that hop
    # (services/orchestrator). Leaving them settable would leave two
    # runtime authorities over one path.
    orchestrator_base_url = os.getenv(
        "ORCHESTRATOR_BASE_URL",
        "http://orchestrator:8700",
    ).strip().rstrip("/")

    parsed_url = urlparse(orchestrator_base_url)

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RuntimeError("ORCHESTRATOR_BASE_URL must be a valid HTTP URL")

    return Settings(
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        orchestrator_base_url=orchestrator_base_url,
    )
