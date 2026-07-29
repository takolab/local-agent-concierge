import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    hermes_api_base_url: str
    hermes_api_server_key: str


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(f"{name} is required")

    return value


def load_settings() -> Settings:
    slack_bot_token = _required_env("SLACK_BOT_TOKEN")
    slack_app_token = _required_env("SLACK_APP_TOKEN")
    hermes_api_server_key = _required_env("HERMES_API_SERVER_KEY")

    if not slack_bot_token.startswith("xoxb-"):
        raise RuntimeError("SLACK_BOT_TOKEN must start with 'xoxb-'")

    if not slack_app_token.startswith("xapp-"):
        raise RuntimeError("SLACK_APP_TOKEN must start with 'xapp-'")

    hermes_api_base_url = os.getenv(
        "HERMES_API_BASE_URL",
        "http://hermes-agent:8642",
    ).strip().rstrip("/")

    parsed_url = urlparse(hermes_api_base_url)

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RuntimeError("HERMES_API_BASE_URL must be a valid HTTP URL")

    return Settings(
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        hermes_api_base_url=hermes_api_base_url,
        hermes_api_server_key=hermes_api_server_key,
    )
