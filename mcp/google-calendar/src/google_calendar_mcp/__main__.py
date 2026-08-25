from google_calendar_mcp.server import mcp
from google_calendar_mcp.telemetry import configure_tracing


def main() -> None:
    configure_tracing()

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
    )


if __name__ == "__main__":
    main()
