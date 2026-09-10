"""Configuration for reaching the Orchestrator.

The Slack Gateway's dispatch address moved from `HERMES_API_BASE_URL` to
`ORCHESTRATOR_BASE_URL`, and its Hermes credential requirement was
removed entirely. These tests pin both halves of that, so a partial
revert -- one authority left controlling the path, or the credential
quietly required again -- cannot pass.

All values are synthetic.
"""

import pytest

from slack_gateway.config import load_settings

SYNTHETIC_BOT_TOKEN = "xoxb-synthetic"
SYNTHETIC_APP_TOKEN = "xapp-synthetic"


@pytest.fixture(autouse=True)
def slack_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SYNTHETIC_BOT_TOKEN)
    monkeypatch.setenv("SLACK_APP_TOKEN", SYNTHETIC_APP_TOKEN)
    monkeypatch.delenv("ORCHESTRATOR_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_API_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_API_SERVER_KEY", raising=False)


def test_orchestrator_base_url_defaults_to_the_compose_service() -> None:
    settings = load_settings()

    assert settings.orchestrator_base_url == "http://orchestrator:8700"


def test_orchestrator_base_url_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ORCHESTRATOR_BASE_URL",
        "  http://synthetic-host:9999/  ",
    )

    settings = load_settings()

    assert settings.orchestrator_base_url == "http://synthetic-host:9999"


@pytest.mark.parametrize(
    "value",
    ["not-a-url", "ftp://synthetic-host:9999", "http://"],
    ids=["no_scheme", "wrong_scheme", "no_host"],
)
def test_invalid_orchestrator_base_url_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", value)

    with pytest.raises(RuntimeError, match="ORCHESTRATOR_BASE_URL"):
        load_settings()


def test_hermes_settings_are_no_longer_required_or_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Gateway holds no Hermes credential and has no direct Hermes path.

    Setting the old variables must not resurrect either one: they are not
    a fallback, and there is nowhere for them to be read.
    """
    monkeypatch.setenv("HERMES_API_BASE_URL", "http://synthetic-hermes:8642")
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "synthetic-key")

    settings = load_settings()

    assert settings.orchestrator_base_url == "http://orchestrator:8700"

    field_values = vars(settings).values()
    assert "synthetic-key" not in field_values
    assert "http://synthetic-hermes:8642" not in field_values
    assert not hasattr(settings, "hermes_api_base_url")
    assert not hasattr(settings, "hermes_api_server_key")


def test_slack_credentials_are_still_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN")

    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        load_settings()
