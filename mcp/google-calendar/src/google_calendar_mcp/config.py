import os
from pathlib import Path


DEFAULT_CREDENTIALS_PATH = Path(
    "/data/google-calendar/credentials.json"
)
DEFAULT_TOKEN_PATH = Path(
    "/data/google-calendar/token.json"
)

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
)


def get_credentials_path() -> Path:
    return Path(
        os.getenv(
            "GOOGLE_CALENDAR_CREDENTIALS_PATH",
            str(DEFAULT_CREDENTIALS_PATH),
        )
    )


def get_token_path() -> Path:
    return Path(
        os.getenv(
            "GOOGLE_CALENDAR_TOKEN_PATH",
            str(DEFAULT_TOKEN_PATH),
        )
    )
