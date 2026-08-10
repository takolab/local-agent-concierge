from google_auth_oauthlib.flow import InstalledAppFlow

from google_calendar_mcp.config import (
    SCOPES,
    get_credentials_path,
    get_token_path,
)


def main() -> None:
    """Run the initial Google OAuth authorization flow."""
    credentials_path = get_credentials_path()
    token_path = get_token_path()

    if not credentials_path.is_file():
        raise RuntimeError(
            f"Google OAuth credentials were not found at "
            f"{credentials_path}."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_path),
        SCOPES,
    )

    credentials = flow.run_local_server(
        host="localhost",
        bind_addr="0.0.0.0",
        port=8080,
        open_browser=False,
        access_type="offline",
        prompt="consent",
    )

    token_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    token_path.write_text(
        credentials.to_json(),
        encoding="utf-8",
    )
    token_path.chmod(0o600)

    print(f"Google OAuth token saved to {token_path}")


if __name__ == "__main__":
    main()
