from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from google_calendar_mcp.config import SCOPES, get_token_path


def load_credentials() -> Credentials:
    """Load and refresh the saved Google OAuth credentials."""
    token_path = get_token_path()

    if not token_path.is_file():
        raise RuntimeError(
            f"Google OAuth token was not found at {token_path}. "
            "Run the OAuth bootstrap command first."
        )

    credentials = Credentials.from_authorized_user_file(
        str(token_path),
        SCOPES,
    )

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_path.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )
        token_path.chmod(0o600)
        return credentials

    raise RuntimeError(
        "Google OAuth credentials are invalid and cannot be refreshed. "
        "Run the OAuth bootstrap command again."
    )
