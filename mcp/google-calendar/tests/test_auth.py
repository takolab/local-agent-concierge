import stat
from unittest.mock import MagicMock

import pytest

from google_calendar_mcp import auth


def test_load_credentials_raises_when_token_file_does_not_exist(
    monkeypatch,
    tmp_path,
):
    token_path = tmp_path / "token.json"

    monkeypatch.setattr(
        auth,
        "get_token_path",
        lambda: token_path,
    )

    with pytest.raises(
        RuntimeError,
        match="Google OAuth token was not found",
    ):
        auth.load_credentials()


def test_load_credentials_returns_valid_credentials(
    monkeypatch,
    tmp_path,
):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")

    fake_credentials = MagicMock()
    fake_credentials.valid = True

    load_from_file = MagicMock(
        return_value=fake_credentials,
    )

    monkeypatch.setattr(
        auth,
        "get_token_path",
        lambda: token_path,
    )

    monkeypatch.setattr(
        auth.Credentials,
        "from_authorized_user_file",
        load_from_file,
    )

    result = auth.load_credentials()

    assert result is fake_credentials

    load_from_file.assert_called_once_with(
        str(token_path),
        auth.SCOPES,
    )


def test_load_credentials_refreshes_expired_credentials(
    monkeypatch,
    tmp_path,
):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")

    fake_credentials = MagicMock()
    fake_credentials.valid = False
    fake_credentials.expired = True
    fake_credentials.refresh_token = "refresh-token"
    fake_credentials.to_json.return_value = (
        '{"token": "refreshed-token"}'
    )

    load_from_file = MagicMock(
        return_value=fake_credentials,
    )

    fake_request = object()

    monkeypatch.setattr(
        auth,
        "get_token_path",
        lambda: token_path,
    )

    monkeypatch.setattr(
        auth.Credentials,
        "from_authorized_user_file",
        load_from_file,
    )

    monkeypatch.setattr(
        auth,
        "Request",
        lambda: fake_request,
    )

    result = auth.load_credentials()

    assert result is fake_credentials

    fake_credentials.refresh.assert_called_once_with(
        fake_request
    )

    assert token_path.read_text(
        encoding="utf-8"
    ) == '{"token": "refreshed-token"}'

    assert stat.S_IMODE(
        token_path.stat().st_mode
    ) == 0o600


def test_load_credentials_raises_when_credentials_cannot_refresh(
    monkeypatch,
    tmp_path,
):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")

    fake_credentials = MagicMock()
    fake_credentials.valid = False
    fake_credentials.expired = True
    fake_credentials.refresh_token = None

    monkeypatch.setattr(
        auth,
        "get_token_path",
        lambda: token_path,
    )

    monkeypatch.setattr(
        auth.Credentials,
        "from_authorized_user_file",
        MagicMock(
            return_value=fake_credentials,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="credentials are invalid and cannot be refreshed",
    ):
        auth.load_credentials()
